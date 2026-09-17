from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.invoicing.service import BillingError, BillingService, BillingValidationService, slice_charge
from app.main import create_app
from app.models import (
    BillingCycle,
    ChargeRecord,
    Invoice,
    InvoiceChargeLink,
    InvoiceLine,
    Project,
)
from app.pricing.bootstrap import seed_demo
from app.rating.engine import RatingEngine
from tests.test_lifecycle_phase2 import T
from tests.test_metering_phase2 import meter_engine


@pytest.fixture
def billing(history):
    history.fake.data["volumes"] = []
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=4))
    meter_engine(history).run()
    with history.sessions.begin() as db:
        seed_demo(db, history.cloud_id, "test")
    history.rating = RatingEngine(history.engine, history.sessions, history.settings, history.manager.gate)
    history.rating.run()
    history.billing = BillingService(history.settings)
    return history


def cycle(h, start=T, end=T + timedelta(hours=4)):
    with h.sessions.begin() as db:
        c = h.billing.create_cycle(
            db, dict(code=uuid4().hex, name="September", period_start=start, period_end=end), "test"
        )
        h.billing.calculate(db, c, "test")
        identifier = c.id
    return identifier


def finalize(h, identifier):
    with h.sessions.begin() as db:
        c = db.get(BillingCycle, identifier)
        h.billing.review_cycle(db, c, "test")
        invoices = list(db.scalars(select(Invoice).where(Invoice.billing_cycle_id == identifier)))
        for i in invoices:
            h.billing.finalize_invoice(db, i, "test")
        h.billing.finalize_cycle(db, c, "test")
        return invoices[0].id


def test_draft_grouping_trace_idempotency(billing):
    h = billing
    cid = cycle(h)
    with h.sessions.begin() as db:
        c = db.get(BillingCycle, cid)
        h.billing.calculate(db, c, "test")
        invoices = list(db.scalars(select(Invoice)))
        assert len(invoices) == 1
        i = invoices[0]
        assert i.status == "DRAFT" and i.invoice_number is None
        assert BillingValidationService(h.billing).validate(db, i)["valid"]
        assert db.scalar(select(func.sum(InvoiceLine.amount))) == i.subtotal
        assert db.scalar(select(func.sum(InvoiceChargeLink.included_amount))) == i.subtotal
        assert db.scalar(select(func.count()).select_from(InvoiceChargeLink)) == db.scalar(
            select(func.count()).select_from(ChargeRecord)
        )
        assert db.scalar(select(func.count()).select_from(InvoiceLine).where(InvoiceLine.amount == 0)) >= 1


def test_finalize_snapshot_adjustments_and_rerating(billing):
    h = billing
    cid = cycle(h)
    iid = finalize(h, cid)
    with h.sessions.begin() as db:
        invoice = db.get(Invoice, iid)
        total = invoice.grand_total
        name = invoice.project_name_snapshot
        number = invoice.invoice_number
        h.billing.finalize_invoice(db, invoice, "test")
        assert invoice.invoice_number == number
        project = db.get(Project, (h.cloud_id, invoice.project_id))
        project.project_name = "Renamed"
        assert invoice.project_name_snapshot == name
        assert all(link.locked_at for link in db.scalars(select(InvoiceChargeLink)))
        with pytest.raises(BillingError):
            h.billing.regenerate(db, invoice, "test")
        for kind, magnitude in [("CREDIT", "100"), ("DEBIT", "50")]:
            a = h.billing.adjustment(
                db,
                invoice,
                dict(
                    type=kind,
                    amount=Decimal(magnitude),
                    currency="VND",
                    reason_code="MANUAL_CORRECTION",
                    reason_text="Test correction",
                ),
                "test",
            )
            assert (
                h.billing.totals(db, invoice)["net_amount"] == total
                if kind == "CREDIT"
                else total - Decimal(100)
            )
            with pytest.raises(BillingError):
                h.billing.adjustment_transition(db, a, "apply", "test")
            h.billing.adjustment_transition(db, a, "approve", "test")
            h.billing.adjustment_transition(db, a, "apply", "test")
            h.billing.adjustment_transition(db, a, "apply", "test")
        assert h.billing.totals(db, invoice)["net_amount"] == total - Decimal(50)
        assert invoice.grand_total == total
        assert BillingValidationService(h.billing).validate(db, invoice)["valid"]
    run = h.rating.run(T, T + timedelta(hours=4), force=True, actor="test")
    assert run.status == "PARTIAL"
    assert all(e["code"] == "BILLED_CHARGE_CONFLICT" for e in run.errors)
    with h.sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(ChargeRecord).where(ChargeRecord.status == "SUPERSEDED")
            )
            == 0
        )


def test_adjacent_cycle_conserves_amounts_and_overlap_blocked(billing):
    h = billing
    first = cycle(h, T, T + timedelta(hours=1, minutes=7))
    second = cycle(h, T + timedelta(hours=1, minutes=7), T + timedelta(hours=4))
    finalize(h, first)
    finalize(h, second)
    with h.sessions() as db:
        assert len(set(db.scalars(select(Invoice.invoice_number)))) == 2
        assert db.scalar(select(func.sum(Invoice.grand_total))) == db.scalar(
            select(func.sum(ChargeRecord.subtotal))
        )
    overlapping = cycle(h)
    with pytest.raises(BillingError) as error:
        finalize(h, overlapping)
    assert any(i["code"] == "CHARGE_ALREADY_BILLED" for i in error.value.issues)


def test_stale_draft_requires_regeneration(billing):
    h = billing
    cid = cycle(h)
    h.rating.run(T, T + timedelta(hours=4), force=True)
    with h.sessions.begin() as db:
        i = db.scalars(select(Invoice)).one()
        result = BillingValidationService(h.billing).validate(db, i)
        assert not result["valid"]
        h.billing.calculate(db, db.get(BillingCycle, cid), "test")
        assert BillingValidationService(h.billing).validate(db, i)["valid"]
    finalize(h, cid)


@pytest.mark.parametrize(
    "corruption,code",
    [
        ("amount", "RECONCILIATION_FAILED"),
        ("currency", "CURRENCY_CONFLICT"),
        ("quantity", "RECONCILIATION_FAILED"),
        ("empty", "EMPTY_INVOICE"),
    ],
)
def test_validation_blocks_corrupt_draft(billing, corruption, code):
    h = billing
    cycle(h)
    with h.sessions.begin() as db:
        i = db.scalars(select(Invoice)).one()
        line = db.scalars(select(InvoiceLine)).first()
        if corruption == "amount":
            line.amount += 1
        if corruption == "currency":
            line.currency = "USD"
        if corruption == "quantity":
            line.usage_quantity += 1
        if corruption == "empty":
            from sqlalchemy import delete

            db.execute(delete(InvoiceChargeLink))
            db.execute(delete(InvoiceLine))
        db.flush()
        result = BillingValidationService(h.billing).validate(db, i)
        assert not result["valid"]
        assert code in {e["code"] for e in result["issues"]}


def test_pending_usage_blocks_invoice(history):
    h = history
    h.fake.data["volumes"] = []
    h.sync_at(T)
    h.fake.data["instances"][0]["status"] = "ERROR"
    h.sync_at(T + timedelta(hours=4))
    meter_engine(h).run()
    h.billing = BillingService(h.settings)
    with h.sessions.begin() as db:
        from app.models import UsageRecord

        u = db.scalars(select(UsageRecord)).first()
        c = h.billing.create_cycle(
            db, dict(code="empty", name="empty", period_start=T, period_end=T + timedelta(hours=4)), "test"
        )
        invoice = Invoice(
            cloud_id=h.cloud_id,
            project_id=u.project_id,
            project_name_snapshot="test",
            billing_cycle_id=c.id,
            billing_timezone=c.billing_timezone,
            currency="VND",
            period_start=c.period_start,
            period_end=c.period_end,
        )
        db.add(invoice)
        db.flush()
        result = BillingValidationService(h.billing).validate(db, invoice)
        assert result["unrated_usage_count"] > 0
        assert not result["valid"]


def test_api_workflow_and_security(billing):
    h = billing
    h.settings.billing_admin_token = "admin-secret"
    h.settings.billing_operator_token = "operator-secret"
    from pydantic import SecretStr

    h.settings.billing_admin_token = SecretStr("admin-secret")
    h.settings.billing_operator_token = SecretStr("operator-secret")
    admin = {"Authorization": "Bearer admin-secret"}
    operator = {"Authorization": "Bearer operator-secret"}
    with TestClient(create_app(h.settings, h.engine, lambda: h.fake)) as client:
        body = dict(name="Test", period_start=T.isoformat(), period_end=(T + timedelta(hours=4)).isoformat())
        assert client.post("/api/v1/billing/cycles", json=body).status_code == 403
        assert (
            client.post(
                "/api/v1/billing/cycles", json={**body, "period_end": T.isoformat()}, headers=operator
            ).status_code
            == 422
        )
        r = client.post("/api/v1/billing/cycles", json=body, headers=operator)
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        base = f"/api/v1/billing/cycles/{cid}"
        assert client.post(base + "/calculate", json={}, headers=operator).status_code == 200
        i = client.get("/api/v1/invoices").json()[0]
        ib = f"/api/v1/invoices/{i['id']}"
        assert client.get(ib).json()["validation"]["valid"]
        assert client.get(ib + "/charges").json()
        assert client.get(ib + "/lines").json()
        assert client.post(base + "/review", json={}, headers=operator).status_code == 200
        assert client.post(ib + "/finalize", json={}, headers=operator).status_code == 403
        result = client.post(ib + "/finalize", json={}, headers=admin)
        assert result.status_code == 200, result.text
        assert result.json()["invoice_number"]
        assert client.post(ib + "/regenerate", json={}, headers=admin).status_code == 409
        assert client.patch(ib, json={"grand_total": "1"}, headers=admin).status_code == 405
        assert client.delete(ib, headers=admin).status_code == 405
        assert client.post(base + "/finalize", json={}, headers=admin).status_code == 200
        assert client.post(base + "/close", json={}, headers=admin).status_code == 200
        a = client.post(
            ib + "/adjustments",
            json=dict(
                type="CREDIT", amount="100", currency="VND", reason_code="SERVICE_CREDIT", reason_text="test"
            ),
            headers=admin,
        )
        assert a.status_code == 200, a.text
        aid = a.json()["id"]
        assert client.post(f"/api/v1/adjustments/{aid}/approve", json={}, headers=admin).status_code == 200
        assert client.post(f"/api/v1/adjustments/{aid}/apply", json={}, headers=admin).status_code == 200
        assert client.get(ib + "/audit").json()
        assert client.get("/api/v1/billing/quality").status_code == 200
        assert client.get(base + "/summary").json()["finalized_invoices"] == 1


def test_rounding_prefix_allocation():
    from types import SimpleNamespace

    c = SimpleNamespace(
        rated_period_start=T,
        rated_period_end=T + timedelta(seconds=3),
        subtotal=Decimal("0.00000001"),
        rated_quantity=Decimal("1"),
    )
    parts = [slice_charge(c, T + timedelta(seconds=n), T + timedelta(seconds=n + 1)) for n in range(3)]
    assert sum(p[2] for p in parts) == c.subtotal
    assert sum(p[3] for p in parts) == c.rated_quantity


def test_multi_price_and_currency_grouping(billing):
    from app.models import ProjectOverride
    from app.pricing.service import create

    h = billing
    with h.sessions.begin() as db:
        c = db.scalars(select(ChargeRecord).where(ChargeRecord.meter_name == "compute.vcpu")).one()
        create(
            db,
            ProjectOverride,
            dict(
                project_id=c.project_id,
                product_id=c.product_id,
                unit_price=Decimal("1200"),
                currency="VND",
                effective_from=T + timedelta(hours=2),
                effective_to=None,
                reason="test",
            ),
            h.cloud_id,
            "test",
        )
    h.rating.run(T, T + timedelta(hours=4), force=True)
    cid = cycle(h)
    with h.sessions.begin() as db:
        cpu = list(db.scalars(select(InvoiceLine).where(InvoiceLine.meter_name == "compute.vcpu")))
        assert len(cpu) == 2
        assert {line.unit_price for line in cpu} == {Decimal("1000"), Decimal("1200")}
        # SQLite-only deliberate currency fixture; production charges are immutable.
        charge = db.scalars(
            select(ChargeRecord).where(
                ChargeRecord.status == "RATED", ChargeRecord.meter_name == "compute.ram"
            )
        ).one()
        charge.currency = "USD"
        db.flush()
        h.billing.calculate(db, db.get(BillingCycle, cid), "test")
        invoices = list(db.scalars(select(Invoice)))
        assert {i.currency for i in invoices} == {"VND", "USD"}
        assert all(BillingValidationService(h.billing).validate(db, i)["valid"] for i in invoices)


def test_provisional_finalization_policy(billing):
    h = billing
    cycle(h)
    with h.sessions() as db, db.no_autoflush:
        charge = db.scalars(select(ChargeRecord)).first()
        charge.source_usage_status = "PROVISIONAL"
        invoice = db.scalars(select(Invoice)).one()
        result = BillingValidationService(h.billing).validate(db, invoice)
        assert "INVOICE_HAS_PROVISIONAL_CHARGES" in {i["code"] for i in result["issues"]}
        assert not result["valid"]


def test_cycle_state_machine_and_atomic_failure(billing, monkeypatch):
    h = billing
    cid = cycle(h)
    with h.sessions.begin() as db:
        c = db.get(BillingCycle, cid)
        with pytest.raises(BillingError):
            h.billing.close(db, c, "test")
        with pytest.raises(BillingError):
            h.billing.finalize_cycle(db, c, "test")
        h.billing.review_cycle(db, c, "test")
        with pytest.raises(BillingError):
            h.billing.calculate(db, c, "test")
        iid = db.scalars(select(Invoice.id)).one()

    def error(*args, **kwargs):
        raise RuntimeError("injected finalization audit failure")

    monkeypatch.setattr("app.invoicing.service.audit", error)
    with pytest.raises(RuntimeError), h.sessions.begin() as db:
        h.billing.finalize_invoice(db, db.get(Invoice, iid), "test")
    with h.sessions() as db:
        i = db.get(Invoice, iid)
        assert i.status == "REVIEW" and i.invoice_number is None
        assert all(link.locked_at is None for link in db.scalars(select(InvoiceChargeLink)))


def test_timezone_period_boundary(billing):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    h = billing
    # Cycle endpoint exactly equals charge start; half-open intersection excludes it.
    cid = cycle(h, T - timedelta(hours=4), T)
    with h.sessions() as db:
        assert not list(db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cid)))
    local = ZoneInfo("Asia/Ho_Chi_Minh")
    with h.sessions.begin() as db:
        c = h.billing.create_cycle(
            db,
            dict(
                code="month",
                name="September",
                period_start=datetime(2026, 9, 1, tzinfo=local),
                period_end=datetime(2026, 10, 1, tzinfo=local),
            ),
            "test",
        )
        from app.metering.math import utc

        assert utc(c.period_end).isoformat() == "2026-09-30T17:00:00+00:00"


def test_cycle_timezone_survives_reload(billing):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.metering.math import utc

    h = billing
    with h.sessions.begin() as db:
        c = h.billing.create_cycle(
            db,
            dict(
                code="reload",
                name="Timezone",
                period_start=datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
                period_end=datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
            ),
            "test",
        )
        cid = c.id
    with h.sessions() as db:
        assert utc(db.get(BillingCycle, cid).period_end).isoformat() == "2026-09-30T17:00:00+00:00"


def test_open_source_usage_blocks_finalization(billing):
    h = billing
    cid = cycle(h)
    # Reopen a billable allocation, then request a cycle extending beyond that observation.
    h.fake.data["instances"][0]["status"] = "ACTIVE"
    h.sync_at(T + timedelta(hours=5))
    extended = cycle(h, T, T + timedelta(hours=6))
    with h.sessions() as db:
        invoice = db.scalars(select(Invoice).where(Invoice.billing_cycle_id == extended)).one()
        result = BillingValidationService(h.billing).validate(db, invoice)
        assert not result["valid"]
        assert "INVOICE_HAS_PROVISIONAL_USAGE" in {i["code"] for i in result["issues"]}
        original = db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cid)).one()
        assert BillingValidationService(h.billing).validate(db, original)["valid"]


def test_unrated_price_gap_blocks_review(billing):
    from app.models import ProjectAssignment
    from app.pricing.service import transition

    h = billing
    cid = cycle(h)
    with h.sessions.begin() as db:
        assignment = db.scalars(select(ProjectAssignment)).one()
        transition(db, ProjectAssignment, assignment.id, "retire", h.cloud_id, "test")
    assert h.rating.run(T, T + timedelta(hours=4), force=True).status == "PARTIAL"
    with h.sessions() as db:
        invoice = db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cid)).one()
        result = BillingValidationService(h.billing).validate(db, invoice)
        assert result["unrated_usage_count"] > 0
        assert "INVOICE_HAS_UNRATED_USAGE" in {i["code"] for i in result["issues"]}


def test_adjustment_input_errors(billing):
    from pydantic import SecretStr

    h = billing
    cid = cycle(h)
    iid = finalize(h, cid)
    h.settings.billing_admin_token = SecretStr("test-only")
    headers = {"Authorization": "Bearer test-only"}
    with TestClient(create_app(h.settings, h.engine, lambda: h.fake)) as client:
        base = dict(type="CREDIT", amount="10", currency="VND", reason_code="OTHER", reason_text="Correction")
        for changed in [{"amount": -1}, {"amount": 1.1}, {"reason_text": " "}, {"type": "UNKNOWN"}]:
            r = client.post(f"/api/v1/invoices/{iid}/adjustments", json={**base, **changed}, headers=headers)
            assert r.status_code == 422, r.text
        r = client.post(
            f"/api/v1/invoices/{iid}/adjustments", json={**base, "currency": "USD"}, headers=headers
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "CURRENCY_CONFLICT"
