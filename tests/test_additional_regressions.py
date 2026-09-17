import threading
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Instance, SyncRun, Volume
from app.openstack.normalize import normalize_instance
from tests.fakes import FakeClient


def test_image_sized_root_is_unknown():
    fake = FakeClient()
    row = fake.data["instances"][0]
    row["flavor"]["disk"] = 0
    result = normalize_instance(row, fake.flavor)
    assert result["root_disk_gb"] is None
    assert "image_sized_root_disk" in result["quality_issues"]


def test_late_page_failure_does_not_persist_partial_changes(runtime):
    runtime.manager.trigger(wait=True)
    runtime.fake.data["instances"][0]["flavor"]["vcpus"] = 999
    runtime.fake.failures["instances"] = TimeoutError("fixture-sensitive")
    run_id = runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(Instance, (runtime.cloud_id, UUID(runtime.fake.data["instances"][0]["id"]))).vcpus == 2
        assert db.get(SyncRun, run_id).status == "PARTIAL"


def test_volume_disappearance_and_restore(runtime):
    runtime.manager.trigger(wait=True)
    volume = runtime.fake.data["volumes"].pop()
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert not db.get(Volume, (runtime.cloud_id, UUID(volume["id"]))).is_missing
    runtime.manager.trigger(wait=True)
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert db.get(Volume, (runtime.cloud_id, UUID(volume["id"]))).is_missing
    runtime.fake.data["volumes"].append(volume)
    runtime.manager.trigger(wait=True)
    with runtime.sessions() as db:
        assert not db.get(Volume, (runtime.cloud_id, UUID(volume["id"]))).is_missing


def test_manual_sync_conflict_and_automatic_start(runtime):
    runtime.settings.sync_enabled = True
    runtime.fake.block = threading.Event()
    app = create_app(runtime.settings, runtime.engine, lambda: runtime.fake)
    try:
        with TestClient(app) as client:
            # Wait for initial scheduler to hold the gate, without depending on cloud timing.
            for _ in range(100):
                if app.state.sync_manager.gate.locked():
                    break
                threading.Event().wait(0.01)
            assert app.state.sync_manager.gate.locked()
            assert client.post("/api/v1/sync", json={}).status_code == 409
            runtime.fake.block.set()
    finally:
        runtime.fake.block.set()
    with runtime.sessions() as db:
        assert list(db.scalars(select(SyncRun)))
