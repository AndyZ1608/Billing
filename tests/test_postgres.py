"""Uses an isolated schema; never drops tables in the configured database's public schema."""

import os
import threading
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.billing.aggregate import cloud_summary, project_summaries
from app.billing.policy import load_policy
from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.models import Base, Observation, SyncRun
from app.sync.engine import SyncBusy, SyncManager
from tests.fakes import FakeClient

pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres_runtime(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    base_engine = make_engine(url)
    schema = "billing_test_" + uuid4().hex
    with base_engine.begin() as db:
        db.execute(CreateSchema(schema))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    rendered = scoped_url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", rendered)
    engine = make_engine(rendered)
    settings = Settings(_env_file=None, database_url=rendered, sync_enabled=False)
    sessions = make_sessions(engine)
    fake = FakeClient()
    first = SyncManager(engine, sessions, settings, lambda: fake)
    other_engine = make_engine(rendered)
    second = SyncManager(other_engine, make_sessions(other_engine), settings, lambda: fake)
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield engine, sessions, settings, fake, first, second
    finally:
        if fake.block:
            fake.block.set()
        first.close()
        second.close()
        engine.dispose()
        other_engine.dispose()
        with base_engine.begin() as db:
            db.execute(DropSchema(schema, cascade=True))
        base_engine.dispose()


def test_postgres_migration_jsonb_and_aggregation(postgres_runtime):
    engine, sessions, settings, fake, first, second = postgres_runtime
    first.trigger(wait=True)
    first.trigger(wait=True)
    with engine.connect() as connection:
        assert not compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert (
            connection.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='resource_observations' "
                    "AND column_name='normalized_payload'"
                )
            )
            == "jsonb"
        )
    with sessions() as db:
        summary = cloud_summary(
            project_summaries(db, settings.openstack_cloud_id, load_policy(settings.billing_policy_path))
        )
        assert summary["vcpu_count"] == 14
        assert summary["cinder_volume_gb"] == 600
        observation = db.scalars(select(Observation)).first()
        assert observation.observed_at.utcoffset().total_seconds() == 0
        assert isinstance(observation.normalized_payload, dict)


def test_postgres_lock_excludes_second_manager_and_recovers_abandoned_run(postgres_runtime):
    engine, sessions, settings, fake, first, second = postgres_runtime
    fake.block = threading.Event()
    first.trigger()
    try:
        with pytest.raises(SyncBusy):
            second.trigger()
    finally:
        fake.block.set()
        first.future.result(timeout=10)
    with sessions.begin() as db:
        stale_id = uuid4()
        db.add(SyncRun(sync_run_id=stale_id, cloud_id=settings.openstack_cloud_id))
    second.trigger(wait=True)
    with sessions() as db:
        stale = db.get(SyncRun, stale_id)
        assert stale.status == "FAILED"
        assert stale.errors[0]["code"] == "interrupted_previous_run"


def test_postgres_closed_history_usage_immutability_and_overlap(postgres_runtime):
    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    from app.metering.engine import MeteringEngine
    from app.models import StatePeriod, UsageRecord

    engine, sessions, settings, fake, first, second = postgres_runtime
    first.trigger(wait=True)
    fake.data["instances"][0]["status"] = "SHUTOFF"
    changed = first.trigger(wait=True)
    with sessions() as db:
        assert db.get(SyncRun, changed).status == "SUCCESS", db.get(SyncRun, changed).errors
        closed = db.scalars(select(StatePeriod).where(StatePeriod.valid_to.is_not(None))).one()
        closed_id = closed.period_id
        clone = {column.key: getattr(closed, column.key) for column in closed.__table__.columns}
    metering = MeteringEngine(engine, sessions, settings)
    run = metering.run()
    assert run.status == "SUCCESS" and run.usage_records_created == 5
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.execute(update(StatePeriod).where(StatePeriod.period_id == closed_id).values(vcpus=99))
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.execute(update(UsageRecord).values(usage_quantity=999))
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.execute(delete(UsageRecord))
    clone["period_id"] = uuid4()
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.add(StatePeriod(**clone))  # duplicate closed bounds violate exclusion, not just open uniqueness
        db.flush()
    engine.dispose()
    with sessions() as db:
        assert len(list(db.scalars(select(UsageRecord)))) == 5  # reconnect preserves history


def test_postgres_metering_shares_sync_lock(postgres_runtime):
    from app.metering.engine import MeteringBusy, MeteringEngine

    engine, sessions, settings, fake, first, second = postgres_runtime
    fake.block = threading.Event()
    first.trigger()
    try:
        with pytest.raises(MeteringBusy):
            MeteringEngine(engine, sessions, settings).run()
    finally:
        fake.block.set()
        first.future.result(timeout=10)


def test_postgres_phase3_immutable_charges_prices_and_rating_lock(postgres_runtime):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.exc import DBAPIError

    from app.core.jobs import JobBusy
    from app.metering.engine import MeteringEngine
    from app.models import ChargeRecord, PriceRule, ProjectAssignment
    from app.pricing.bootstrap import seed_demo
    from app.rating.engine import RatingEngine

    engine, sessions, settings, fake, first, second = postgres_runtime
    point = [datetime(2026, 9, 1, 10, tzinfo=UTC)]
    first.clock = lambda: point[0]
    first.trigger(wait=True)
    point[0] += timedelta(hours=2)
    fake.data["instances"][0]["status"] = "ERROR"
    first.trigger(wait=True)
    MeteringEngine(engine, sessions, settings).run()
    with sessions.begin() as db:
        book = seed_demo(db, settings.openstack_cloud_id)
    rating = RatingEngine(engine, sessions, settings)
    assert rating.run().status == "SUCCESS"
    with sessions() as db:
        charge = db.scalars(select(ChargeRecord).where(ChargeRecord.meter_name == "compute.vcpu")).first()
        identity = charge.id
        original = charge.subtotal
        rule = db.scalars(select(PriceRule)).first()
        rule_id = rule.id
        version_id = rule.price_book_version_id
    for sql, params in (
        ("UPDATE charge_records SET subtotal=0 WHERE id=:id", {"id": identity}),
        ("DELETE FROM charge_records WHERE id=:id", {"id": identity}),
        ("UPDATE price_rules SET unit_price=5 WHERE id=:id", {"id": rule_id}),
        (
            "UPDATE price_book_versions SET effective_from=effective_from - interval '1 day' WHERE id=:id",
            {"id": version_id},
        ),
    ):
        with pytest.raises(DBAPIError), engine.begin() as db:
            db.execute(text(sql), params)
    # Native overlap exclusion even if a caller bypasses API validation.
    with pytest.raises(DBAPIError), sessions.begin() as db:
        db.add(
            ProjectAssignment(
                cloud_id=settings.openstack_cloud_id,
                project_id=None,
                price_book_id=book.id,
                effective_from=point[0],
            )
        )
    with rating.lock.held():
        with pytest.raises(JobBusy):
            RatingEngine(engine, sessions, settings).run()
        with pytest.raises(SyncBusy):
            second.trigger(wait=True)
    rerun = rating.run(point[0] - timedelta(hours=2), point[0], True, "pg-admin")
    assert rerun.status == "SUCCESS"
    engine.dispose()
    with sessions() as db:
        old = db.get(ChargeRecord, identity)
        assert old.status == "SUPERSEDED" and old.subtotal == original
        assert old.superseded_by_rating_run_id == rerun.id
        assert (
            db.scalar(select(func.count()).select_from(ChargeRecord).where(ChargeRecord.status == "RATED"))
            == 5
        )


def test_postgres_phase3_upgrade_preserves_phase2_rows(postgres_runtime):
    from datetime import UTC, datetime, timedelta

    from app.metering.engine import MeteringEngine
    from app.models import Instance, MeteringRun, Project, StatePeriod, UsageRecord, Volume
    from app.pricing.bootstrap import seed_demo
    from app.rating.engine import RatingEngine

    engine, sessions, settings, fake, first, second = postgres_runtime
    command.downgrade(Config("alembic.ini"), "0002")
    point = [datetime(2026, 9, 1, 10, tzinfo=UTC)]
    first.clock = lambda: point[0]
    first.trigger(wait=True)
    point[0] += timedelta(hours=2)
    fake.data["instances"][0]["status"] = "ERROR"
    first.trigger(wait=True)
    MeteringEngine(engine, sessions, settings).run()

    def saved():
        with sessions() as db:
            return {
                model.__tablename__: [
                    {c.key: getattr(r, c.key) for c in model.__table__.columns}
                    for r in db.scalars(select(model).order_by(*model.__table__.primary_key.columns))
                ]
                for model in (Project, Instance, Volume, Observation, StatePeriod, UsageRecord, MeteringRun)
            }

    before = saved()
    assert len(before["usage_records"]) == 5
    command.upgrade(Config("alembic.ini"), "head")
    assert saved() == before
    with sessions.begin() as db:
        seed_demo(db, settings.openstack_cloud_id)
    assert RatingEngine(engine, sessions, settings).run().status == "SUCCESS"
    assert saved() == before


def test_postgres_phase4_upgrade_locks_and_immutability(postgres_runtime):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from sqlalchemy.exc import DBAPIError

    from app.invoicing.service import BillingService
    from app.metering.engine import MeteringEngine
    from app.models import (
        BillingCycle,
        Invoice,
        InvoiceChargeLink,
        InvoiceLine,
    )
    from app.pricing.bootstrap import seed_demo
    from app.rating.engine import RatingEngine

    engine, sessions, settings, fake, first, second = postgres_runtime
    command.downgrade(Config("alembic.ini"), "0003")
    fake.data["instances"] = fake.data["instances"][:1]
    fake.data["volumes"] = []
    start = datetime(2026, 9, 1, 10, tzinfo=UTC)
    point = [start]
    first.clock = lambda: point[0]
    first.trigger(wait=True)
    point[0] += timedelta(hours=2)
    fake.data["instances"][0]["status"] = "ERROR"
    first.trigger(wait=True)
    MeteringEngine(engine, sessions, settings).run()
    with sessions.begin() as db:
        seed_demo(db, settings.openstack_cloud_id)
    rating = RatingEngine(engine, sessions, settings)
    assert rating.run().status == "SUCCESS"

    def saved():
        new_tables = {
            "billing_cycles",
            "invoices",
            "invoice_lines",
            "invoice_charge_links",
            "billing_adjustments",
            "billing_audit_log",
            "invoice_number_counters",
        }
        with sessions() as db:
            return {
                table.name: list(db.execute(select(table).order_by(*table.primary_key.columns)).mappings())
                for table in Base.metadata.sorted_tables
                if table.name not in new_tables
            }

    before = saved()
    command.upgrade(Config("alembic.ini"), "head")
    assert saved() == before
    billing = BillingService(settings)
    with rating.lock.held(), sessions.begin() as db:
        c = billing.create_cycle(
            db, dict(code="PG", name="PG cycle", period_start=start, period_end=point[0]), "pg-admin"
        )
        billing.calculate(db, c, "pg-admin")
        billing.calculate(db, c, "pg-admin")
        billing.review_cycle(db, c, "pg-admin")
        invoice = db.scalars(select(Invoice)).one()
        billing.finalize_invoice(db, invoice, "pg-admin")
        billing.finalize_cycle(db, c, "pg-admin")
        iid, cid = invoice.id, c.id
        line = db.scalars(select(InvoiceLine)).first()
        lid = line.id
        link = db.scalars(select(InvoiceChargeLink)).first()
        linkid = link.id
        chargeid = link.charge_record_id
        a = billing.adjustment(
            db,
            invoice,
            dict(
                type="CREDIT", amount=Decimal("100"), currency="VND", reason_code="OTHER", reason_text="Test"
            ),
            "pg-admin",
        )
        billing.adjustment_transition(db, a, "approve", "pg-admin")
        billing.adjustment_transition(db, a, "apply", "pg-admin")
        aid = a.id
        number = invoice.invoice_number
    for sql, identity in [
        ("UPDATE invoices SET notes='altered' WHERE id=:id", iid),
        ("DELETE FROM invoices WHERE id=:id", iid),
        ("UPDATE invoice_lines SET amount=amount+1 WHERE id=:id", lid),
        ("DELETE FROM invoice_lines WHERE id=:id", lid),
        ("UPDATE invoice_charge_links SET locked_at=NULL WHERE id=:id", linkid),
        ("DELETE FROM invoice_charge_links WHERE id=:id", linkid),
        ("UPDATE billing_adjustments SET amount=-1 WHERE id=:id", aid),
        ("DELETE FROM billing_adjustments WHERE id=:id", aid),
        ("UPDATE billing_cycles SET status='OPEN' WHERE id=:id", cid),
        ("UPDATE charge_records SET status='SUPERSEDED' WHERE id=:id", chargeid),
    ]:
        with pytest.raises(DBAPIError), engine.begin() as db:
            db.execute(text(sql), {"id": identity})
    with pytest.raises(DBAPIError), engine.begin() as db:
        db.execute(text("DELETE FROM billing_audit_log"))
    with pytest.raises(DBAPIError), engine.begin() as db:
        db.execute(text("UPDATE invoice_number_counters SET value=0"))
    # PostgreSQL rejects overlapping billed slices even when bypassing service validation.
    with sessions.begin() as db:
        overlap = billing.create_cycle(
            db, dict(code="OVERLAP", name="Overlap test", period_start=start, period_end=point[0]), "test"
        )
        billing.calculate(db, overlap, "test")
        draft = db.scalars(select(Invoice).where(Invoice.billing_cycle_id == overlap.id)).one()
        draft_id = draft.id
    with pytest.raises(DBAPIError) as conflict, sessions.begin() as db:
        candidate = db.scalars(
            select(InvoiceChargeLink).where(InvoiceChargeLink.invoice_id == draft_id)
        ).first()
        candidate.locked_at = point[0]
        db.flush()
    assert conflict.value.orig.diag.constraint_name == "ex_billed_charge_period"

    # A second application process uses the same advisory lock for billing mutations.
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.main import create_app

    settings.billing_admin_token = SecretStr("pg-test-admin")
    with TestClient(create_app(settings, engine, lambda: fake)) as client:
        with rating.lock.held():
            blocked = client.post(
                f"/api/v1/invoices/{iid}/finalize", json={}, headers={"Authorization": "Bearer pg-test-admin"}
            )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "BILLING_BUSY"
        repeated = client.post(
            f"/api/v1/invoices/{iid}/finalize", json={}, headers={"Authorization": "Bearer pg-test-admin"}
        )
        assert repeated.status_code == 200
        assert repeated.json()["invoice_number"] == number

    assert rating.run(start, point[0], True, "pg-admin").errors[0]["code"] == "BILLED_CHARGE_CONFLICT"
    engine.dispose()
    with sessions.begin() as db:
        invoice = db.get(Invoice, iid)
        billing.finalize_invoice(db, invoice, "pg-admin")
        assert invoice.invoice_number == number
        assert billing.totals(db, invoice)["net_amount"] == invoice.grand_total - Decimal("100")
        billing.close(db, db.get(BillingCycle, cid), "pg-admin")
