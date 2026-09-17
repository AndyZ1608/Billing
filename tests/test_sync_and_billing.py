import json
import threading
from uuid import UUID, uuid4

import pytest
from openstack.exceptions import HttpException
from sqlalchemy import func, select

from app.billing.aggregate import cloud_summary, project_summaries
from app.billing.policy import Dimensions
from app.models import Cloud, Instance, Observation, Project, SyncRun, Volume
from app.sync.engine import SyncBusy


def total(runtime):
    with runtime.sessions() as db:
        return cloud_summary(project_summaries(db, runtime.cloud_id, runtime.policy))


def test_multi_project_aggregation_and_idempotency(runtime):
    runtime.manager.trigger(wait=True)
    first = total(runtime)
    runtime.fake.data["instances"].append(runtime.fake.data["instances"][0])
    runtime.manager.trigger(wait=True)
    assert first == total(runtime)
    assert first["projects"] == 2
    assert first["instance_count"] == 3
    assert first["vcpu_count"] == 14
    assert first["ram_gb"] == 28
    assert first["nova_root_disk_gb"] == 60  # volume-boot VM's flavor disk is not billed
    assert first["nova_ephemeral_disk_gb"] == 30
    assert first["cinder_volume_count"] == 2
    assert first["cinder_volume_gb"] == 600
    with runtime.sessions() as db:
        rows = project_summaries(db, runtime.cloud_id, runtime.policy)
        project_a = next(row for row in rows if row["project_name"] == "Project A")
        assert project_a["vcpu_count"] == 6
        assert project_a["ram_gb"] == 12
        assert project_a["cinder_volume_gb"] == 100
        assert db.scalar(select(func.count()).select_from(Instance)) == 3
        # Phase 2 retains project snapshots but deduplicates unchanged resource observations.
        assert db.scalar(select(func.count()).select_from(Observation)) == 9


def test_state_policy_and_per_dimension_rules(runtime):
    runtime.manager.trigger(wait=True)
    runtime.policy.nova.counted_states = ["ACTIVE"]
    runtime.policy.cinder.counted_states = ["in-use"]
    assert total(runtime)["vcpu_count"] == 10
    assert total(runtime)["cinder_volume_gb"] == 500
    runtime.policy.nova.counted_states.append("SHUTOFF")
    runtime.policy.nova.state_metrics["SHUTOFF"] = Dimensions(vcpu=False, ram=False)
    summary = total(runtime)
    assert summary["instance_count"] == 3
    assert summary["vcpu_count"] == 10
    assert summary["ram_gb"] == 20
    assert summary["nova_root_disk_gb"] == 60


def test_rename_unknown_project_and_observation_history(runtime):
    runtime.manager.trigger(wait=True)
    runtime.fake.data["projects"][0]["name"] = "Renamed"
    orphan = uuid4()
    runtime.fake.data["instances"][0]["project_id"] = str(orphan)
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(Project, (runtime.cloud_id, UUID("a" * 32))).project_name == "Renamed"
        assert db.get(Project, (runtime.cloud_id, orphan)).is_placeholder
        assert db.scalar(select(func.count()).select_from(Project)) == 3
        old = db.scalars(select(Observation).where(Observation.resource_type == "projects")).all()
        assert {row.normalized_payload["project_name"] for row in old} >= {"Project A", "Renamed"}


def test_missing_requires_multiple_complete_scans_and_reappearance(runtime):
    runtime.manager.trigger(wait=True)
    vm = runtime.fake.data["instances"].pop(0)
    runtime.manager.trigger(wait=True)
    assert total(runtime)["instance_count"] == 3
    assert total(runtime)["pending_missing_instances"] == 1
    runtime.fake.failures["instances"] = TimeoutError("secret never logged")
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(Instance, (runtime.cloud_id, UUID(vm["id"]))).missing_scans == 1
    runtime.fake.failures.clear()
    runtime.manager.trigger(wait=True)
    runtime.manager.trigger(wait=True)
    assert total(runtime)["instance_count"] == 2
    with runtime.sessions() as db:
        missing = db.get(Instance, (runtime.cloud_id, UUID(vm["id"])))
        assert missing.is_missing
        assert missing.deleted_at_openstack is None  # polling cannot prove a deletion timestamp
    runtime.fake.data["instances"].append(vm)
    runtime.manager.trigger(wait=True)
    assert total(runtime)["instance_count"] == 3
    assert total(runtime)["pending_missing_instances"] == 0


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_service_failure_preserves_inventory_and_marks_partial(runtime, status):
    runtime.manager.trigger(wait=True)
    runtime.fake.data["volumes"] = []
    runtime.fake.failures["volumes"] = HttpException("sensitive-body", http_status=status)
    run_id = runtime.manager.trigger(wait=True)
    assert total(runtime)["cinder_volume_gb"] == 600
    with runtime.sessions() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "PARTIAL"
        assert run.errors[0]["code"] == f"http_{status}"
        assert "sensitive-body" not in json.dumps(run.errors)
        assert all(volume.missing_scans == 0 for volume in db.scalars(select(Volume)))


def test_auth_failure_and_recovery(runtime):
    runtime.fake.auth_error = HttpException("password-secret", http_status=401)
    failed = runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(SyncRun, failed).status == "FAILED"
        assert db.get(Cloud, runtime.cloud_id).connection_status == "FAILED"
    runtime.fake.auth_error = None
    runtime.manager.trigger(wait=True)
    assert total(runtime)["vcpu_count"] == 14


def test_malformed_row_does_not_remove_previous_inventory(runtime):
    runtime.manager.trigger(wait=True)
    runtime.fake.data["instances"][0]["project_id"] = "invalid"
    run_id = runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(SyncRun, run_id).status == "PARTIAL"
        assert all(row.missing_scans == 0 for row in db.scalars(select(Instance)))
    assert total(runtime)["vcpu_count"] == 14


def test_single_run_lock(runtime):
    barrier = threading.Event()
    runtime.fake.block = barrier
    runtime.manager.trigger()
    try:
        with pytest.raises(SyncBusy):
            runtime.manager.trigger()
    finally:
        barrier.set()
        runtime.manager.future.result(timeout=10)
    runtime.manager.trigger(wait=True)


def test_secrets_excluded_from_observations(runtime):
    vm = runtime.fake.data["instances"][0]
    vm.update(admin_password="top-secret", user_data="top-secret", metadata={"secret": "top-secret"})
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        for row in db.scalars(select(Observation)):
            assert "top-secret" not in json.dumps(row.normalized_payload)
        for row in db.scalars(select(Instance)):
            assert "top-secret" not in json.dumps(row.raw_payload)


def test_unknown_quantities_flagged_and_statuses_stored(runtime):
    runtime.fake.data["instances"][0]["flavor"] = {"id": "missing"}
    runtime.fake.data["instances"][1]["status"] = "ERROR"
    runtime.manager.trigger(wait=True)
    summary = total(runtime)
    assert summary["instance_count"] == 2
    assert summary["vcpu_count"] == 8
    assert summary["incomplete_instances"] == 1
    with runtime.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Instance)) == 3
