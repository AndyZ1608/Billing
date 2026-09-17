from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.metering.math import utc
from app.models import Instance, LifecycleHead, Observation, StatePeriod, SyncRun, Volume

T = datetime(2026, 9, 1, 10, tzinfo=UTC)


def periods(runtime, kind="INSTANCE"):
    with runtime.sessions() as db:
        return list(
            db.scalars(
                select(StatePeriod).where(StatePeriod.resource_type == kind).order_by(StatePeriod.valid_from)
            )
        )


def test_baseline_and_unchanged_observations(history):
    history.sync_at(T)
    history.sync_at(T + timedelta(hours=1))
    rows = periods(history)
    assert len(rows) == 1
    assert rows[0].history_confidence == "BASELINE"
    assert utc(rows[0].valid_from) == T
    assert utc(rows[0].openstack_created_at) < T
    assert rows[0].valid_to is None
    with history.sessions() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Observation).where(Observation.resource_type == "instances")
            )
            == 1
        )
        head = db.scalars(select(LifecycleHead).where(LifecycleHead.resource_type == "INSTANCE")).one()
        assert utc(head.last_observed_at) == T + timedelta(hours=1)


def test_state_resize_and_ownership_history(history):
    history.sync_at(T)
    row = history.fake.data["instances"][0]
    row["status"] = "SHUTOFF"
    history.sync_at(T + timedelta(hours=2))
    row["flavor"].update(id="resized", vcpus=4, ram=8192, disk=80, ephemeral=20)
    history.sync_at(T + timedelta(hours=4))
    row["project_id"] = history.fake.data["projects"][1]["id"]
    history.sync_at(T + timedelta(hours=5))
    rows = periods(history)
    assert [p.state for p in rows] == ["ACTIVE", "SHUTOFF", "SHUTOFF", "SHUTOFF"]
    assert [p.vcpus for p in rows] == [2, 2, 4, 4]
    assert rows[0].root_disk_gb == 20 and rows[2].root_disk_gb == 80
    for previous, next_period in zip(rows, rows[1:]):
        assert previous.valid_to == next_period.valid_from
    assert rows[-1].project_id != rows[-2].project_id


def test_volume_extend_state_type_and_delete(history):
    history.sync_at(T)
    row = history.fake.data["volumes"][0]
    row.update(size=200, status="in-use", volume_type="premium")
    history.sync_at(T + timedelta(hours=4))
    history.fake.data["volumes"] = []
    for minutes in (360, 365, 370):
        history.sync_at(T + timedelta(minutes=minutes))
    rows = periods(history, "VOLUME")
    assert [p.volume_size_gb for p in rows] == [100, 200]
    assert rows[1].volume_type == "premium" and rows[1].state == "in-use"
    assert utc(rows[1].valid_to) == T + timedelta(hours=6)
    assert utc(rows[1].deletion_confirmed_at) == T + timedelta(minutes=370)


def test_pending_reappearance_keeps_period_confirmed_reappearance_preserves_gap(history):
    history.sync_at(T)
    vm = history.fake.data["instances"].pop()
    history.sync_at(T + timedelta(minutes=5))
    assert periods(history)[0].valid_to is None
    history.fake.data["instances"].append(vm)
    history.sync_at(T + timedelta(minutes=10))
    assert len(periods(history)) == 1
    history.fake.data["instances"] = []
    for minute in (15, 20, 25):
        history.sync_at(T + timedelta(minutes=minute))
    closed = periods(history)[0]
    assert utc(closed.valid_to) == T + timedelta(minutes=15)
    assert utc(closed.deletion_confirmed_at) == T + timedelta(minutes=25)
    assert closed.closure_reason == "CONFIRMED_DISAPPEARANCE"
    history.fake.data["instances"].append(vm)
    history.sync_at(T + timedelta(minutes=30))
    rows = periods(history)
    assert len(rows) == 2 and rows[0].valid_to == closed.valid_to
    assert utc(rows[1].valid_from) == T + timedelta(minutes=30)
    with history.sessions() as db:
        current = db.get(Instance, (history.cloud_id, UUID(vm["id"])))
        assert current.missing_scans == 0 and current.deleted_confirmed_at is None


@pytest.mark.parametrize(
    "kind,resource_type,model", [("instances", "INSTANCE", Instance), ("volumes", "VOLUME", Volume)]
)
def test_failed_service_preserves_ledger_and_missing_count(history, kind, resource_type, model):
    history.sync_at(T)
    history.fake.data[kind] = []
    history.fake.failures[kind] = TimeoutError("private wire body")
    history.sync_at(T + timedelta(hours=2))
    assert periods(history, resource_type)[0].valid_to is None
    with history.sessions() as db:
        assert db.scalars(select(model)).one().missing_scans == 0


def test_deleted_flavor_keeps_only_previously_known_same_flavor_allocation(history):
    history.sync_at(T)
    history.fake.data["instances"][0]["flavor"] = {"id": "small"}
    history.sync_at(T + timedelta(hours=1))
    assert len(periods(history)) == 1
    with history.sessions() as db:
        current = db.scalars(select(Instance)).one()
        assert current.vcpus == 2 and "retained_known_vcpus" in current.quality_issues
    history.fake.data["instances"][0]["flavor"] = {"id": "different-unavailable-flavor"}
    history.sync_at(T + timedelta(hours=2))
    rows = periods(history)
    assert rows[0].vcpus == 2 and rows[1].vcpus is None


def test_explicit_deletion_timestamp(history):
    history.sync_at(T)
    history.fake.data["instances"][0].update(
        status="DELETED", terminated_at=(T + timedelta(hours=1)).isoformat()
    )
    history.sync_at(T + timedelta(hours=2))
    assert utc(periods(history)[0].valid_to) == T + timedelta(hours=1)
    assert periods(history)[0].closure_reason == "OPENSTACK_DELETION"
    history.sync_at(T + timedelta(hours=3))
    assert len(periods(history)) == 1


def test_same_time_conflicting_change_rolls_back_resource_transaction(history, monkeypatch):
    history.sync_at(T)
    history.fake.data["instances"][0]["status"] = "SHUTOFF"
    monkeypatch.setattr("app.sync.engine.utcnow", lambda: T)
    run_id = history.manager.trigger(wait=True)
    with history.sessions() as db:
        assert db.get(SyncRun, run_id).errors[0]["code"] == "lifecycle_inconsistency"
        assert db.scalars(select(Instance)).one().status == "ACTIVE"
    assert len(periods(history)) == 1 and periods(history)[0].valid_to is None


def test_deletion_timestamp_at_baseline_falls_back_to_observation(history):
    history.sync_at(T)
    history.fake.data["instances"][0].update(status="DELETED", terminated_at=T.isoformat())
    history.sync_at(T + timedelta(hours=1))
    row = periods(history)[0]
    assert utc(row.valid_to) == T + timedelta(hours=1)
    assert row.closure_reason == "OBSERVED_DELETION"
