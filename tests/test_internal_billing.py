from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.billing.internal import report
from app.metering.engine import MeteringEngine
from app.models import ChargeRecord, Instance, Project, SyncRun
from app.pricing.internal import seed_internal
from app.rating.engine import RatingEngine
from tests.test_lifecycle_phase2 import T


def setup_vm(h, cpu, ram, disk):
    h.fake.data["volumes"] = []
    vm = h.fake.data["instances"][0]
    vm.update(image={"id": "image"})
    vm["flavor"].update(vcpus=cpu, ram=ram * 1024, disk=disk, ephemeral=0)
    h.sync_at(T)


def rate(h, end):
    MeteringEngine(h.engine, h.sessions, h.settings, h.manager.gate).run()
    with h.sessions.begin() as db:
        seed_internal(db, h.settings, T)
    RatingEngine(h.engine, h.sessions, h.settings, h.manager.gate).run()
    with h.sessions() as db:
        return report(db, h.settings, T, end, now=end, trace=True)


@pytest.mark.parametrize(
    "cpu,ram,disk,duration,expected",
    [
        (4, 8, 50, 120, ("80000", "176000", "50000", "306000")),
        (2, 4, 20, 30, ("10000", "22000", "5000", "37000")),
    ],
)
def test_internal_golden(history, cpu, ram, disk, duration, expected):
    h = history
    setup_vm(h, cpu, ram, disk)
    h.fake.data["instances"][0]["status"] = "ERROR"
    end = T + timedelta(minutes=duration)
    h.sync_at(end)
    result = rate(h, end)
    assert result["cost"] == dict(zip(("cpu", "ram", "ssd", "total"), map(Decimal, expected)))
    assert result["estimated_cost"]["total"] == 0
    assert result["unrated_segments"] == 0
    with h.sessions.begin() as db:
        seed_internal(db, h.settings, T)
        before = db.scalar(select(func.count()).select_from(ChargeRecord))
    RatingEngine(h.engine, h.sessions, h.settings, h.manager.gate).run()
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChargeRecord)) == before


def test_internal_resize(history):
    h = history
    setup_vm(h, 2, 4, 20)
    h.fake.data["instances"][0]["flavor"].update(vcpus=4, ram=8192)
    h.sync_at(T + timedelta(hours=2))
    h.fake.data["instances"][0]["status"] = "ERROR"
    end = T + timedelta(hours=5)
    h.sync_at(end)
    result = rate(h, end)
    assert result["usage"] == {
        "vcpu_hours": Decimal(16),
        "ram_gib_hours": Decimal(32),
        "ssd_gib_hours": Decimal(100),
    }
    assert result["cost"] == dict(
        cpu=Decimal(160000), ram=Decimal(352000), ssd=Decimal(50000), total=Decimal(562000)
    )


def test_open_usage_is_estimated_not_persisted(history):
    h = history
    setup_vm(h, 4, 8, 50)
    end = T + timedelta(hours=2)
    result = rate(h, end)
    assert result["cost"]["total"] == Decimal(306000)
    assert result["estimated_cost"]["total"] == Decimal(306000)
    assert result["estimated"]
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChargeRecord)) == 0


def test_boot_volume_not_double_counted(history):
    h = history
    vm = h.fake.data["instances"][0]
    vol = h.fake.data["volumes"][0]
    vm["image"] = ""
    vm["flavor"].update(vcpus=4, ram=8192, disk=50, ephemeral=0)
    vol.update(size=50, status="available", attachments=[{"server_id": vm["id"], "device": "/dev/vda"}])
    h.sync_at(T)
    end = T + timedelta(hours=2)
    vm["status"] = "ERROR"
    vol["status"] = "error"
    h.sync_at(end)
    result = rate(h, end)
    assert result["cost"]["total"] == Decimal(306000)
    assert result["cost"]["ssd"] == Decimal(50000)
    assert result["instances"][0]["cost"]["ssd"] == Decimal(50000)
    assert result["instances"][0]["current"]["local_root_gib"] == 0


def test_cross_project_counts_and_same_names(runtime, monkeypatch):
    from copy import deepcopy

    h = runtime
    projects = h.fake.data["projects"]
    third = deepcopy(projects[0])
    third.update(id=str(uuid4()), name="Project C")
    projects.append(third)
    template = h.fake.data["instances"][0]
    h.fake.data["instances"] = []
    h.fake.data["volumes"] = []
    for p, count in zip(projects, (2, 3, 1)):
        for _ in range(count):
            vm = deepcopy(template)
            vm.update(id=str(uuid4()), project_id=p["id"], name="web01")
            vm["flavor"].update(vcpus=2, ram=4096, disk=20, ephemeral=0)
            h.fake.data["instances"].append(vm)
    monkeypatch.setattr("app.sync.engine.utcnow", lambda: T)
    h.manager.trigger(wait=True)
    with h.sessions() as db:
        run = db.scalars(select(SyncRun)).one()
        assert run.services["nova"]["per_project"] == {
            p["id"]: count for p, count in zip(projects, (2, 3, 1))
        }
        assert db.scalar(select(func.count()).select_from(Instance)) == 6
    for vm in h.fake.data["instances"]:
        vm["status"] = "ERROR"
    end = T + timedelta(hours=1)
    monkeypatch.setattr("app.sync.engine.utcnow", lambda: end)
    h.manager.trigger(wait=True)
    result = rate(h, end)
    totals = {str(p["project_id"]): p["cost"]["total"] for p in result["projects"]}
    assert totals == {p["id"]: Decimal(74000) * count for p, count in zip(projects, (2, 3, 1))}
    assert result["cost"]["total"] == sum(totals.values())
    with h.sessions.begin() as db:
        db.get(Project, (h.cloud_id, UUID(projects[0]["id"]))).project_name = "Renamed"
    with h.sessions() as db:
        r = report(db, h.settings, T, end, project_id=UUID(projects[0]["id"]), now=end)
        assert r["projects"][0]["project_name"] == "Renamed"
        assert r["cost"]["total"] == Decimal(148000)


def test_project_alias_conflict_and_null_flavor():
    from app.openstack.normalize import MalformedResource, normalize_instance
    from tests.fakes import FakeClient

    vm = FakeClient().data["instances"][0]
    vm["tenant_id"] = str(uuid4())
    with pytest.raises(MalformedResource):
        normalize_instance(vm, lambda _: {})
    del vm["tenant_id"]
    vm["flavor"] = {"id": "existing", "vcpus": None, "ram": None, "disk": None}
    fetch = MagicMock(return_value={"vcpus": 4, "ram": 8192, "disk": 50, "ephemeral": 0})
    resolved = normalize_instance(vm, fetch)
    assert (resolved["vcpus"], resolved["ram_mb"], resolved["root_disk_gb"]) == (4, 8192, 50)


def test_installed_sdk_wire_mapping():
    from openstack.block_storage.v3.volume import Volume
    from openstack.compute.v2.server import Server

    assert Server._query_mapping._mapping["all_projects"] == "all_tenants"
    assert Volume._query_mapping._mapping["all_projects"] == "all_tenants"


def test_internal_api_and_failure_completeness(history):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import StatePeriod
    from app.openstack.diagnostics import diagnostics

    h = history
    setup_vm(h, 4, 8, 50)
    end = T + timedelta(hours=2)
    h.fake.data["instances"][0]["status"] = "ERROR"
    h.sync_at(end)
    rate(h, end)
    app = create_app(h.settings, h.engine, lambda: h.fake)
    with TestClient(app) as client:
        params = {"start": T.isoformat(), "end": end.isoformat()}
        summary = client.get("/api/v1/billing/summary", params=params)
        assert summary.status_code == 200
        assert Decimal(summary.json()["cost"]["total"]) == Decimal(306000)
        vm_id = h.fake.data["instances"][0]["id"]
        detail = client.get(f"/api/v1/billing/instances/{vm_id}", params=params)
        assert detail.status_code == 200
        assert detail.json()["trace_total"] > 0
        assert Decimal(detail.json()["cost"]["total"]) == Decimal(306000)
        assert client.get("/api/v1/billing/instances", params={"start": T.isoformat()}).status_code == 422
        assert client.get("/api/v1/diagnostics/openstack").status_code == 200
    with h.sessions() as db:
        before = db.scalar(select(func.count()).select_from(StatePeriod))
    h.fake.failures["instances"] = RuntimeError("temporary Nova failure")
    h.sync_at(end + timedelta(minutes=5))
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StatePeriod)) == before
        assert diagnostics(db, h.settings)["data_quality_status"] in ("PARTIAL", "INCOMPLETE")
        assert not db.scalars(select(Instance)).first().is_missing


def test_volume_detachment_preserves_vm_cost_history(history):
    h = history
    vm = h.fake.data["instances"][0]
    vol = h.fake.data["volumes"][0]
    vm["flavor"].update(vcpus=2, ram=4096, disk=0, ephemeral=0)
    vm["image"] = ""
    vol.update(size=50, attachments=[{"server_id": vm["id"], "device": "/dev/vda"}])
    h.sync_at(T)
    vol["attachments"] = []
    h.sync_at(T + timedelta(hours=1))
    end = T + timedelta(hours=2)
    result = rate(h, end)
    assert result["cost"]["ssd"] == Decimal(50000)
    assert result["instances"][0]["cost"]["ssd"] == Decimal(25000)
    assert result["instances"][0]["current"]["cinder_volume_gib"] == 0
