from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.metering.engine import MeteringBusy, MeteringEngine, PolicyVersionConflict
from app.metering.math import quantity, seconds, split_days, utc
from app.metering.query import totals, usage_view
from app.models import MeteringPolicyVersion, UsageRecord
from tests.test_lifecycle_phase2 import T, periods


def meter_engine(history):
    return MeteringEngine(history.engine, history.sessions, history.settings, history.manager.gate)


def calculate(history, start, end, version="meter-v1", **kwargs):
    with history.sessions() as db:
        rows, issues, _ = usage_view(db, history.cloud_id, version, start, end, **kwargs)
        return totals(rows), rows, issues


def test_compute_golden_and_repeated_metering(history):
    vm = history.fake.data["instances"][0]
    vm["flavor"].update(vcpus=4, ram=8192, disk=50, ephemeral=0)
    history.fake.data["volumes"] = []
    history.sync_at(T)
    vm["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=2))
    engine = meter_engine(history)
    run = engine.run(now=T + timedelta(hours=3))
    assert run.status == "SUCCESS" and run.usage_records_created == 5
    result, rows, issues = calculate(history, T, T + timedelta(hours=2))
    assert result["instance_hours"] == Decimal("2")
    assert result["vcpu_hours"] == Decimal("8")
    assert result["ram_gib_hours"] == Decimal("16")
    assert result["root_disk_gib_hours"] == Decimal("100")
    assert not issues and all(row["status"] == "FINAL" for row in rows)
    assert engine.run(now=T + timedelta(hours=4)).state_periods_processed == 0
    replay = engine.run(T + timedelta(minutes=10), T + timedelta(minutes=20), force=True)
    assert replay.usage_records_created == 0 and replay.usage_records_reused == 5
    with history.sessions() as db:
        assert db.scalar(select(func.count()).select_from(UsageRecord)) == 5


def test_resize_golden(history):
    history.fake.data["volumes"] = []
    history.sync_at(T)
    history.fake.data["instances"][0]["flavor"].update(id="bigger", vcpus=4, ram=8192)
    history.sync_at(T + timedelta(hours=2))
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=5))
    assert meter_engine(history).run().status == "SUCCESS"
    result, _, _ = calculate(history, T, T + timedelta(hours=5))
    assert result["vcpu_hours"] == Decimal("16")
    assert result["ram_gib_hours"] == Decimal("32")


def test_volume_golden(history):
    history.fake.data["instances"] = []
    history.sync_at(T)
    history.fake.data["volumes"][0]["size"] = 200
    history.sync_at(T + timedelta(hours=4))
    history.fake.data["volumes"][0]["status"] = "error"
    history.sync_at(T + timedelta(hours=6))
    assert meter_engine(history).run().status == "SUCCESS"
    result, _, _ = calculate(history, T, T + timedelta(hours=6))
    assert result["volume_hours"] == Decimal("6")
    assert result["volume_gib_hours"] == Decimal("800")


def test_partial_hour_and_query_intersection(history):
    history.fake.data["volumes"] = []
    history.sync_at(T - timedelta(hours=2))
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=10))
    meter_engine(history).run()
    result, rows, _ = calculate(history, T, T + timedelta(hours=2))
    assert result["instance_hours"] == Decimal("2")
    assert result["vcpu_hours"] == Decimal("4")
    assert all(row["period_start"] == T and row["period_end"] == T + timedelta(hours=2) for row in rows)
    short, _, _ = calculate(history, T, T + timedelta(minutes=15))
    assert short["instance_hours"] == Decimal("0.25")


def test_open_usage_is_provisional_and_does_not_close_or_persist(history):
    history.fake.data["volumes"] = []
    history.sync_at(T)
    engine = meter_engine(history)
    assert engine.run(now=T + timedelta(hours=1)).usage_records_created == 0
    result, rows, _ = calculate(history, T, T + timedelta(hours=8), now=T + timedelta(hours=4, minutes=30))
    assert result["instance_hours"] == Decimal("4.5")
    assert result["provisional"]["vcpu_hours"] == Decimal("9")
    assert all(row["status"] == "PROVISIONAL" for row in rows)
    assert periods(history)[0].valid_to is None
    with history.sessions() as db:
        assert db.scalar(select(func.count()).select_from(UsageRecord)) == 0


def test_late_confirmed_deletion_after_watermark(history):
    history.fake.data["volumes"] = []
    history.sync_at(T)
    history.fake.data["instances"] = []
    history.sync_at(T + timedelta(hours=1))
    engine = meter_engine(history)
    engine.run(now=T + timedelta(hours=2))
    history.sync_at(T + timedelta(hours=3))
    history.sync_at(T + timedelta(hours=4))
    run = engine.run(now=T + timedelta(hours=5))
    assert run.usage_records_created == 5
    result, _, _ = calculate(history, T, T + timedelta(hours=5))
    assert result["instance_hours"] == Decimal("1")
    assert result["vcpu_hours"] == Decimal("2")


def test_cross_midnight_canonical_records(history):
    start = datetime(2026, 9, 1, 22, tzinfo=UTC)
    history.fake.data["volumes"] = []
    history.sync_at(start)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(start + timedelta(hours=4))
    run = meter_engine(history).run()
    assert run.usage_records_created == 10
    with history.sessions() as db:
        records = list(
            db.scalars(
                select(UsageRecord)
                .where(UsageRecord.meter_name == "compute.instance")
                .order_by(UsageRecord.period_start)
            )
        )
        assert [r.usage_quantity for r in records] == [Decimal("2"), Decimal("2")]
        assert utc(records[0].period_end) == datetime(2026, 9, 2, tzinfo=UTC)


@pytest.mark.parametrize("day,hours", [(datetime(2026, 3, 8), 23), (datetime(2026, 11, 1), 25)])
def test_dst_day_has_actual_elapsed_duration(day, hours):
    zone = ZoneInfo("America/New_York")
    start = day.replace(tzinfo=zone)
    end = (day + timedelta(days=1)).replace(tzinfo=zone)
    chunks = list(split_days(start, end, "America/New_York"))
    assert len(chunks) == 1
    assert quantity(1, *chunks[0]) == Decimal(hours)


def test_microsecond_precision_without_float():
    assert seconds(T, T + timedelta(microseconds=1)) == Decimal("0.000001")
    assert quantity(Decimal("1.5"), T, T + timedelta(minutes=30)) == Decimal("0.75")


def test_volume_boot_history_does_not_double_count_root(history):
    history.fake.data["instances"][0]["image"] = ""
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.fake.data["volumes"][0]["status"] = "error"
    history.sync_at(T + timedelta(hours=2))
    meter_engine(history).run()
    result, _, _ = calculate(history, T, T + timedelta(hours=2))
    assert result["root_disk_gib_hours"] == Decimal("0")
    assert result["volume_gib_hours"] == Decimal("200")


def test_unknown_state_is_excluded_and_reported(history):
    history.fake.data["instances"][0]["status"] = "ALIEN_STATE"
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ACTIVE"
    history.sync_at(T + timedelta(hours=1))
    run = meter_engine(history).run()
    assert run.status == "PARTIAL"
    result, _, issues = calculate(history, T, T + timedelta(hours=1))
    assert result["vcpu_hours"] == Decimal("0")
    assert issues[0]["codes"] == ["unknown_instance_state"]


def test_policy_version_binding_and_new_version_remeter(history):
    history.fake.data["volumes"] = []
    history.fake.data["instances"][0]["status"] = "SHUTOFF"
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=1))
    engine = meter_engine(history)
    engine.run()
    engine.policy.instance["SHUTOFF"].vcpu = False
    with pytest.raises(PolicyVersionConflict):
        engine.run()
    engine.version = "meter-v2"
    assert engine.run(T, T + timedelta(hours=1), force=True).status == "SUCCESS"
    original, _, _ = calculate(history, T, T + timedelta(hours=1))
    revised, _, _ = calculate(history, T, T + timedelta(hours=1), version="meter-v2")
    assert original["vcpu_hours"] == Decimal("2") and revised["vcpu_hours"] == Decimal("0")
    with history.sessions() as db:
        assert db.scalar(select(func.count()).select_from(MeteringPolicyVersion)) == 2


def test_metering_cannot_overlap_sync_gate(history):
    engine = meter_engine(history)
    history.manager.gate.acquire()
    try:
        with pytest.raises(MeteringBusy):
            engine.run()
    finally:
        history.manager.gate.release()


def test_metering_rolls_back_records_and_watermark_on_failure(history, monkeypatch):
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=1))
    engine = meter_engine(history)

    def fail(*args):
        raise ValueError("secret must not leak")

    monkeypatch.setattr("app.metering.engine.quantity", fail)
    run = engine.run()
    assert run.status == "FAILED" and "secret" not in str(run.errors)
    with history.sessions() as db:
        assert db.scalar(select(func.count()).select_from(UsageRecord)) == 0
        assert db.get(MeteringPolicyVersion, (history.cloud_id, "meter-v1")).watermark is None


def test_historical_api_filters_audit_and_validation(history):
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "ERROR"
    history.sync_at(T + timedelta(hours=2))
    app = create_app(history.settings, history.engine, lambda: history.fake)
    with TestClient(app) as client:
        assert client.post("/api/v1/metering/run", json={}).json()["status"] == "SUCCESS"
        params = {
            "start": T.isoformat(),
            "end": (T + timedelta(hours=2)).isoformat(),
            "resource_type": "INSTANCE",
        }
        result = client.get("/api/v1/metering/summary", params=params).json()
        assert result["vcpu_hours"] == "4.000000000000"
        assert len(result["daily"]) == 1 and result["timezone"] == "Asia/Ho_Chi_Minh"
        pid = history.fake.data["projects"][0]["id"]
        vmid = history.fake.data["instances"][0]["id"]
        for path in ("projects", f"projects/{pid}", f"projects/{pid}/resources"):
            assert client.get(f"/api/v1/metering/{path}", params=params).status_code == 200
        page = client.get(f"/api/v1/metering/resources/INSTANCE/{vmid}/usage", params=params)
        assert page.status_code == 200, page.text
        row = page.json()["items"][0]
        record = client.get(f"/api/v1/metering/records/{row['usage_record_id']}")
        assert record.status_code == 200 and isinstance(record.json()["usage_quantity"], str)
        assert client.get(f"/api/v1/metering/observations/{row['source_observation_id']}").status_code == 200
        lifecycle = client.get(f"/api/v1/metering/resources/INSTANCE/{vmid}/lifecycle").json()
        assert lifecycle["total"] == 2
        assert client.get("/api/v1/metering/quality").json()["lifecycle_inconsistencies"] == 0
        assert (
            client.get("/api/v1/metering/summary", params={**params, "end": T.isoformat()}).status_code == 422
        )
        assert (
            client.get(
                "/api/v1/metering/summary", params={**params, "start": "2026-09-01T10:00:00"}
            ).status_code
            == 422
        )
        assert client.get("/api/v1/metering/summary", params={**params, "meter": "bad"}).status_code == 422
        assert client.get("/api/v1/metering/summary", params={**params, "timezone": "bad"}).status_code == 422
        assert client.post("/api/v1/metering/run", json={"force": True}).status_code == 422
        for path in ("/history", "/quality", "/metering-runs", f"/resources/INSTANCE/{vmid}"):
            assert client.get(path).status_code == 200
        calendar = client.get(
            "/api/v1/metering/calendar-range",
            params={"start_date": "2026-09-01", "end_date": "2026-09-02", "timezone": "Asia/Ho_Chi_Minh"},
        ).json()
        assert calendar["start"] == "2026-08-31T17:00:00+00:00"


def test_resource_and_meter_filter_never_merges_projects(history):
    history.sync_at(T)
    engine = meter_engine(history)
    engine.run()
    pid = UUID(history.fake.data["projects"][1]["id"])
    result, rows, _ = calculate(history, T, T + timedelta(hours=1), project_id=pid)
    assert not rows and result["vcpu_hours"] == 0
    result, rows, _ = calculate(history, T, T + timedelta(hours=1), meter="compute.vcpu")
    assert len(rows) == 1 and result["volume_gib_hours"] == 0
