from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.metering.math import utc
from app.models import (
    ChargeRecord,
    PriceBook,
    PriceBookVersion,
    PriceRule,
    PricingAudit,
    Product,
    ProjectAssignment,
    ProjectOverride,
)
from app.pricing.bootstrap import seed_demo
from app.pricing.service import PricingError, create, transition
from app.rating.engine import RatingEngine
from app.rating.query import summarize
from tests.test_lifecycle_phase2 import T
from tests.test_metering_phase2 import meter_engine

PROJECT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
HEADERS = {"X-Pricing-Admin": "true", "X-Audit-Actor": "test-admin", "Content-Type": "application/json"}


@pytest.fixture
def rated(history):
    history.fake.data["volumes"] = []
    history.fake.data["instances"][0]["flavor"].update(vcpus=4, ram=8192)
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=4))
    meter_engine(history).run()
    history.rating = RatingEngine(history.engine, history.sessions, history.settings, history.manager.gate)
    with history.sessions.begin() as db:
        history.product = create(
            db,
            Product,
            dict(
                code="CPU",
                name="vCPU",
                meter_name="compute.vcpu",
                unit="vCPU-hour",
                service_category="compute",
            ),
            history.cloud_id,
            "test",
        )
        history.book = create(
            db, PriceBook, dict(code="STANDARD", name="Standard", currency="VND"), history.cloud_id, "test"
        )
        create(
            db,
            ProjectAssignment,
            dict(price_book_id=history.book.id, effective_from=T - timedelta(days=1)),
            history.cloud_id,
            "test",
        )
    return history


def price(h, amount, start=T, end=None, book=None, version="one"):
    with h.sessions.begin() as db:
        v = create(
            db,
            PriceBookVersion,
            dict(price_book_id=(book or h.book).id, version=version, effective_from=start, effective_to=end),
            h.cloud_id,
            "test",
        )
        create(
            db,
            PriceRule,
            dict(
                price_book_version_id=v.id,
                product_id=h.product.id,
                unit_price=Decimal(amount),
                billing_unit="vCPU-hour",
            ),
            h.cloud_id,
            "test",
        )
        transition(db, PriceBookVersion, v.id, "activate", h.cloud_id, "test")
        return v


def charges(h, meter="compute.vcpu", all_status=False):
    with h.sessions() as db:
        query = select(ChargeRecord).where(ChargeRecord.meter_name == meter)
        if not all_status:
            query = query.where(ChargeRecord.status != "SUPERSEDED")
        return list(db.scalars(query.order_by(ChargeRecord.rated_period_start, ChargeRecord.created_at)))


def test_price_boundary_golden_and_idempotency(rated):
    price(rated, "1000", end=T + timedelta(hours=2))
    price(rated, "1500", start=T + timedelta(hours=2), version="two")
    run = rated.rating.run()
    assert run.status == "PARTIAL"  # Other meters have no catalog mapping yet, and stay visible.
    rows = charges(rated)
    assert [r.rated_quantity for r in rows] == [Decimal(8), Decimal(8)]
    assert [r.subtotal for r in rows] == [Decimal(8000), Decimal(12000)]
    assert sum(r.subtotal for r in rows) == Decimal(20000)
    ids = [r.id for r in rows]
    repeated = rated.rating.run()
    assert repeated.charge_records_created == 0
    assert [r.id for r in charges(rated)] == ids


def test_assignment_boundary_and_override(rated):
    price(rated, "2000")
    with rated.sessions.begin() as db:
        enterprise = create(
            db, PriceBook, dict(code="ENTERPRISE", name="Enterprise", currency="VND"), rated.cloud_id, "test"
        )
        create(
            db,
            ProjectAssignment,
            dict(
                project_id=PROJECT,
                price_book_id=rated.book.id,
                effective_from=T,
                effective_to=T + timedelta(hours=2),
            ),
            rated.cloud_id,
            "test",
        )
        create(
            db,
            ProjectAssignment,
            dict(project_id=PROJECT, price_book_id=enterprise.id, effective_from=T + timedelta(hours=2)),
            rated.cloud_id,
            "test",
        )
    price(rated, "1000", book=enterprise)
    rated.rating.run()
    rows = charges(rated)
    assert [r.subtotal for r in rows] == [Decimal(16000), Decimal(8000)]
    assert [r.price_book_id for r in rows] == [rated.book.id, enterprise.id]
    with rated.sessions.begin() as db:
        override = create(
            db,
            ProjectOverride,
            dict(
                project_id=PROJECT,
                product_id=rated.product.id,
                unit_price=Decimal("1500"),
                currency="VND",
                reason="test override",
                effective_from=T,
            ),
            rated.cloud_id,
            "test",
        )
    rated.rating.run(T, T + timedelta(hours=4), True, "test")
    assert all(
        r.pricing_source == "PROJECT_OVERRIDE"
        and r.project_price_override_id == override.id
        and r.unit_price == Decimal(1500)
        for r in charges(rated)
    )


def test_gap_and_zero_and_retry(rated):
    price(rated, "0", end=T + timedelta(hours=1))
    rated.rating.run()
    rows = charges(rated)
    assert rows[0].status == "RATED" and rows[0].subtotal == Decimal(0)
    assert rows[1].status == "UNRATED" and rows[1].subtotal is None and rows[1].unrated_reason == "PRICE_GAP"
    price(rated, "2000", start=T + timedelta(hours=1), version="gap-filled")
    rated.rating.run()
    assert sum(r.subtotal for r in charges(rated)) == Decimal(24000)
    assert any(r.status == "SUPERSEDED" for r in charges(rated, all_status=True))


def test_force_rerating_keeps_financial_audit(rated):
    old_version = price(rated, "1000")
    rated.rating.run()
    original = charges(rated)[0]
    with rated.sessions.begin() as db:
        transition(db, PriceBookVersion, old_version.id, "retire", rated.cloud_id, "test")
    price(rated, "1200", version="corrected")
    rated.rating.run()
    assert charges(rated)[0].subtotal == Decimal(16000)
    run = rated.rating.run(T + timedelta(minutes=10), T + timedelta(minutes=20), True, "finance-admin")
    assert charges(rated)[0].subtotal == Decimal(19200)
    with rated.sessions() as db:
        prior = db.get(ChargeRecord, original.id)
        assert prior.status == "SUPERSEDED" and prior.subtotal == Decimal(16000)
        assert prior.superseded_by_rating_run_id == run.id
        assert (
            db.scalar(
                select(func.count())
                .select_from(PricingAudit)
                .where(PricingAudit.action == "FORCE_RERATING_STARTED")
            )
            == 1
        )
    assert utc(charges(rated)[0].rated_period_start) == T  # Replay uses whole canonical usage.


def test_currency_mismatch_is_unrated_and_currencies_never_sum(rated):
    price(rated, "2000", end=T + timedelta(hours=2))
    with rated.sessions.begin() as db:
        other = create(db, PriceBook, dict(code="USD", name="USD", currency="USD"), rated.cloud_id, "test")
        create(
            db,
            ProjectAssignment,
            dict(project_id=PROJECT, price_book_id=other.id, effective_from=T + timedelta(hours=2)),
            rated.cloud_id,
            "test",
        )
    price(rated, "2", book=other)
    rated.rating.run()
    with rated.sessions() as db:
        result = summarize(
            db,
            dict(
                cloud=rated.cloud_id,
                usage_version="meter-v1",
                start=T,
                end=T + timedelta(hours=4),
                timezone="Asia/Ho_Chi_Minh",
                meter="compute.vcpu",
            ),
        )
    assert [(r["currency"], r["total_charge"]) for r in result] == [
        ("USD", Decimal(16)),
        ("VND", Decimal(16000)),
    ]
    with rated.sessions.begin() as db:
        create(
            db,
            ProjectOverride,
            dict(
                project_id=PROJECT,
                product_id=rated.product.id,
                unit_price=Decimal("1"),
                currency="USD",
                reason="wrong currency before assignment",
                effective_from=T,
                effective_to=T + timedelta(hours=2),
            ),
            rated.cloud_id,
            "test",
        )
    rated.rating.run(T, T + timedelta(hours=4), True)
    assert charges(rated)[0].unrated_reason == "CURRENCY_MISMATCH"


def test_exact_query_clipping_and_daily_cost(rated):
    price(rated, "2000")
    rated.rating.run()
    with rated.sessions() as db:
        result = summarize(
            db,
            dict(
                cloud=rated.cloud_id,
                usage_version="meter-v1",
                start=T,
                end=T + timedelta(hours=1),
                timezone="Asia/Ho_Chi_Minh",
                meter="compute.vcpu",
            ),
        )[0]
    assert result["total_charge"] == Decimal(8000)
    assert sum(r["total_charge"] for r in result["daily"]) == Decimal(8000)
    assert result["meters"][0]["rated_quantity"] == Decimal(4)


def test_failed_usage_does_not_half_supersede_or_stop_other_records(rated, monkeypatch):
    price(rated, "1000")
    rated.rating.run()
    original = charges(rated)[0]
    persist = rated.rating.persist_usage

    def fail_after_write(db, usage, resolver, run, force):
        result = persist(db, usage, resolver, run, force)
        if usage.meter_name == "compute.vcpu":
            raise RuntimeError("private error body")
        return result

    monkeypatch.setattr(rated.rating, "persist_usage", fail_after_write)
    result = rated.rating.run(T, T + timedelta(hours=4), True)
    assert result.status == "PARTIAL" and result.errors[0]["code"] == "usage_rating_failed"
    assert charges(rated)[0].id == original.id
    assert charges(rated)[0].status == "RATED"
    assert result.usage_records_processed == 5


def test_overlap_and_active_mutation_validation(rated):
    v = price(rated, "1000")
    with pytest.raises(PricingError, match="overlap"):
        price(rated, "1500", version="overlapping")
    with rated.sessions.begin() as db, pytest.raises(PricingError, match="immutable"):
        create(
            db,
            PriceRule,
            dict(
                price_book_version_id=v.id,
                product_id=rated.product.id,
                unit_price=Decimal(5),
                billing_unit="vCPU-hour",
            ),
            rated.cloud_id,
            "test",
        )


def test_pricing_and_rating_api_workflow(history):
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=5))
    meter_engine(history).run()
    app = create_app(history.settings, history.engine, lambda: history.fake)
    with TestClient(app) as client:

        def post(path, data):
            result = client.post("/api/v1/" + path, json=data, headers=HEADERS)
            assert result.status_code in (200, 201), result.text
            return result.json()

        product = post(
            "pricing/products",
            dict(
                code="CPU",
                name="CPU",
                meter_name="compute.vcpu",
                unit="vCPU-hour",
                service_category="compute",
            ),
        )
        book = post("pricing/price-books", dict(code="STANDARD", name="Standard", currency="VND"))
        version = post(
            f"pricing/price-books/{book['id']}/versions", dict(version="one", effective_from=T.isoformat())
        )
        post(
            f"pricing/price-book-versions/{version['id']}/rules",
            dict(product_id=product["id"], unit_price="2000", billing_unit="vCPU-hour"),
        )
        post(f"pricing/price-book-versions/{version['id']}/activate", {})
        post(
            "pricing/project-assignments",
            dict(project_id=str(PROJECT), price_book_id=book["id"], effective_from=T.isoformat()),
        )
        run = post("rating/run", {})
        assert run["charge_records_created"] > 0
        q = {"start": T.isoformat(), "end": (T + timedelta(hours=5)).isoformat(), "meter": "compute.vcpu"}
        summary = client.get("/api/v1/rating/summary", params=q).json()
        assert Decimal(summary["currencies"][0]["total_charge"]) == Decimal("20000")
        charge = client.get("/api/v1/rating/charges", params=q).json()["items"][0]
        audit = client.get("/api/v1/rating/charges/" + charge["id"]).json()
        assert audit["original_usage"]["usage_record_id"] == charge["usage_record_id"]
        assert audit["price_book_version"] == "one" and audit["source_usage_status"] == "FINAL"
        assert client.post("/api/v1/rating/run", json={}).status_code == 403
        assert client.post("/api/v1/rating/run", headers=HEADERS, json={"force": True}).status_code == 422
        assert (
            client.post(
                "/api/v1/pricing/price-books",
                headers=HEADERS,
                json={"code": "BAD", "name": "Bad", "currency": "XXX"},
            ).status_code
            == 422
        )
        post(
            "pricing/project-overrides",
            dict(
                project_id=str(PROJECT),
                product_id=product["id"],
                unit_price="1500",
                currency="VND",
                reason="test",
                effective_from=T.isoformat(),
            ),
        )
        post("rating/run", dict(start=q["start"], end=q["end"], force=True))
        cost = client.get("/api/v1/rating/projects/" + str(PROJECT), params=q).json()
        assert Decimal(cost["currencies"][0]["total_charge"]) == Decimal("15000")
        for endpoint in (
            "pricing/products",
            "pricing/price-books",
            "pricing/project-assignments",
            "pricing/project-overrides",
            "pricing/audit",
            "rating/runs",
            "rating/quality",
        ):
            assert client.get("/api/v1/" + endpoint).status_code == 200


def test_bootstrap_explicit_idempotent_and_zero_instance_price(rated):
    with rated.sessions.begin() as db:
        # Existing custom default is preserved by optional bootstrap.
        book = seed_demo(db, rated.cloud_id)
        assert seed_demo(db, rated.cloud_id).id == book.id
        assert db.scalar(select(func.count()).select_from(PriceBook).where(PriceBook.code == "POC-VND")) == 1
        assignment = db.scalar(select(ProjectAssignment).where(ProjectAssignment.project_id.is_(None)))
        assert assignment.price_book_id == rated.book.id


def test_normal_gap_retry_never_reprices_already_rated_segment(rated):
    version = price(rated, "1000", end=T + timedelta(hours=1))
    rated.rating.run()
    original = charges(rated)[0]
    with rated.sessions.begin() as db:
        transition(db, PriceBookVersion, version.id, "retire", rated.cloud_id, "test")
    price(rated, "1200", version="replacement")
    rated.rating.run()
    current = charges(rated)
    assert current[0].id == original.id and current[0].subtotal == Decimal(4000)
    assert current[1].subtotal == Decimal(14400)
    assert sum(r.subtotal for r in current) == Decimal(18400)


def test_ram_zero_hour_meters_and_no_provisional_charges(history):
    history.fake.data["volumes"] = []
    history.fake.data["instances"][0]["flavor"].update(vcpus=5, ram=8192)
    history.sync_at(T)
    rating = RatingEngine(history.engine, history.sessions, history.settings)
    with history.sessions.begin() as db:
        seed_demo(db, history.cloud_id)
    assert rating.run().charge_records_created == 0
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=2))
    meter_engine(history).run()
    with history.sessions.begin() as db:
        ram = db.scalar(select(Product).where(Product.meter_name == "compute.ram"))
        create(
            db,
            ProjectOverride,
            dict(
                project_id=PROJECT,
                product_id=ram.id,
                unit_price=Decimal(500),
                currency="VND",
                reason="golden RAM",
                effective_from=T,
            ),
            history.cloud_id,
            "test",
        )
        cpu = db.scalar(select(Product).where(Product.meter_name == "compute.vcpu"))
        create(
            db,
            ProjectOverride,
            dict(
                project_id=PROJECT,
                product_id=cpu.id,
                unit_price=Decimal(2000),
                currency="VND",
                reason="golden CPU",
                effective_from=T,
            ),
            history.cloud_id,
            "test",
        )
    assert rating.run().status == "SUCCESS"
    assert charges(history, "compute.ram")[0].subtotal == Decimal(8000)
    assert charges(history, "compute.vcpu")[0].subtotal == Decimal(20000)
    zero = charges(history, "compute.instance")[0]
    assert zero.subtotal == 0 and zero.status == "RATED" and zero.source_usage_status == "FINAL"


def test_rating_batches_and_version_replay(rated):
    price(rated, "2000")
    rated.rating.batch_size = 1
    first = rated.rating.run()
    assert first.usage_records_processed == 5
    rated.rating.version = "rating-v2"
    rated.rating.run()
    assert charges(rated)[0].rating_version == "rating-v1"
    rated.rating.run(T, T + timedelta(hours=4), True)
    assert charges(rated)[0].rating_version == "rating-v2"
    assert len(charges(rated)) == 1


def test_admin_token_and_invalid_price_strings(rated):
    settings = rated.settings.model_copy(
        update={"pricing_admin_token": __import__("pydantic").SecretStr("test-only")}
    )
    with TestClient(create_app(settings, rated.engine, lambda: rated.fake)) as client:
        assert client.post("/api/v1/rating/run", json={}, headers=HEADERS).status_code == 403
        auth = {**HEADERS, "Authorization": "Bearer test-only"}
        assert client.post("/api/v1/rating/run", json={}, headers=auth).status_code == 200
        path = "/api/v1/pricing/project-overrides"
        base = dict(
            project_id=str(PROJECT),
            product_id=str(rated.product.id),
            currency="VND",
            reason="test",
            effective_from=T.isoformat(),
        )
        for invalid in ("-1", "NaN", "Infinity", "0.000000001", 1.5):
            assert client.post(path, json={**base, "unit_price": invalid}, headers=auth).status_code == 422
        for invalid in ("2026-09-01T10:00:00", "invalid"):
            assert (
                client.post(
                    path, json={**base, "unit_price": "1", "effective_from": invalid}, headers=auth
                ).status_code
                == 422
            )


def test_price_gap_does_not_fall_back_to_default(rated):
    price(rated, "2000")
    with rated.sessions.begin() as db:
        empty = create(
            db,
            PriceBook,
            dict(code="EMPTY", name="Empty project book", currency="VND"),
            rated.cloud_id,
            "test",
        )
        create(
            db,
            ProjectAssignment,
            dict(project_id=PROJECT, price_book_id=empty.id, effective_from=T),
            rated.cloud_id,
            "test",
        )
    rated.rating.run()
    assert charges(rated)[0].unrated_reason == "PRICE_GAP"
    assert charges(rated)[0].price_book_id == empty.id


@pytest.mark.parametrize("day,hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_currency_day_dst_and_cross_midnight_exactness(history, day, hours):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from app.metering.math import utc
    from app.rating.money import cost, display_money

    zone = ZoneInfo("America/New_York")
    start = datetime.combine(datetime.fromisoformat(day).date(), time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    history.fake.data["volumes"] = []
    history.sync_at(utc(start))
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(utc(end))
    meter_engine(history).run(now=utc(end) + timedelta(seconds=1))
    with history.sessions.begin() as db:
        seed_demo(db, history.cloud_id)
    rating = RatingEngine(history.engine, history.sessions, history.settings)
    rating.run()
    with history.sessions() as db:
        report = summarize(
            db,
            dict(
                cloud=history.cloud_id,
                usage_version="meter-v1",
                start=utc(start),
                end=utc(end),
                timezone=str(zone),
                meter="compute.vcpu",
            ),
        )[0]
    assert len(report["daily"]) == 1
    assert report["total_charge"] == Decimal(2 * hours * 1000)
    assert report["monthly"][0]["total_charge"] == report["total_charge"]
    assert cost(Decimal(4), T, T + timedelta(seconds=5400), Decimal(2000)) == Decimal(12000)
    assert cost(Decimal(1), T, T + timedelta(microseconds=1), Decimal("0.12345678")) == Decimal("0.00000000")
    assert display_money(Decimal("1.005"), "USD") == Decimal("1.00")
    assert display_money(Decimal("1.5"), "VND") == Decimal("2")


def test_one_hundred_instance_hours_with_explicit_zero_price(history):
    history.fake.data["volumes"] = []
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=100))
    meter_engine(history).run(now=T + timedelta(hours=101))
    with history.sessions.begin() as db:
        seed_demo(db, history.cloud_id)
    result = RatingEngine(history.engine, history.sessions, history.settings).run()
    assert result.status == "SUCCESS"
    records = charges(history, "compute.instance")
    assert sum(r.rated_quantity for r in records) == Decimal(100)
    assert all(r.status == "RATED" and r.subtotal == Decimal(0) for r in records)
