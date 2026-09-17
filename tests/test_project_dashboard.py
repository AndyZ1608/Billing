from copy import deepcopy
from datetime import timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Instance
from tests.test_lifecycle_phase2 import T


def test_project_first_paging_search_and_counts(history):
    h = history
    template = h.fake.data["instances"][0]
    projects = h.fake.data["projects"]
    h.fake.data["volumes"] = []
    h.fake.data["instances"] = []
    for p, project in enumerate(projects):
        for i in range(100):
            vm = deepcopy(template)
            vm.update(
                id=str(UUID(int=1000 + p * 100 + i)),
                name=f"web-{i:03}",
                project_id=project["id"],
                status="ACTIVE" if i % 2 == 0 else "SHUTOFF",
            )
            h.fake.data["instances"].append(vm)
    h.sync_at(T)
    params = {"start": T.isoformat(), "end": (T + timedelta(hours=1)).isoformat()}
    with TestClient(create_app(h.settings, h.engine, lambda: h.fake)) as client:
        summary = client.get("/api/v1/billing/summary", params=params).json()
        assert "instances" not in summary
        assert int(summary["current"]["active_vm_count"]) == 100
        page = client.get("/api/v1/billing/projects", params=params).json()
        assert page["total"] == 2
        assert all(r["instance_count"] == 100 and r["active_vm_count"] == 50 for r in page["items"])
        scope = {**params, "project_id": projects[0]["id"], "current_only": True}
        first = client.get("/api/v1/billing/instances", params=scope).json()
        assert first["total"] == 100 and len(first["items"]) == 20
        assert all(r["project_id"] == projects[0]["id"] for r in first["items"])
        second = client.get("/api/v1/billing/instances", params={**scope, "offset": 20}).json()
        assert not ({r["instance_id"] for r in first["items"]} & {r["instance_id"] for r in second["items"]})
        for size in (50, 100):
            assert (
                len(client.get("/api/v1/billing/instances", params={**scope, "limit": size}).json()["items"])
                == size
            )
        assert (
            client.get("/api/v1/billing/instances", params={**scope, "status": "ACTIVE"}).json()["total"]
            == 50
        )
        assert (
            client.get("/api/v1/billing/instances", params={**scope, "status": "OTHER"}).json()["total"] == 0
        )
        for query in ("web-000", first["items"][0]["instance_id"]):
            found = client.get("/api/v1/billing/instances", params={**scope, "q": query}).json()
            assert found["total"] == 1
            assert found["items"][0]["project_id"] == projects[0]["id"]
        for sort in ("active_vm_count", "total_cost"):
            assert client.get("/api/v1/billing/projects", params={**params, "sort": sort}).status_code == 200
        assert client.get("/sync").status_code == 200


def test_empty_projects_and_missing_vms_remain_consistent(history):
    h = history
    h.fake.data["volumes"] = []
    h.sync_at(T)
    with h.sessions.begin() as db:
        db.scalar(select(Instance)).is_missing = True
    params = {"start": T.isoformat(), "end": (T + timedelta(hours=1)).isoformat()}
    with TestClient(create_app(h.settings, h.engine, lambda: h.fake)) as client:
        projects = client.get("/api/v1/billing/projects", params=params).json()
        assert projects["total"] == 2
        assert all(r["instance_count"] == 0 and r["active_vm_count"] == 0 for r in projects["items"])
        current = client.get("/api/v1/billing/instances", params={**params, "current_only": True}).json()
        assert current["total"] == 0
        history_page = client.get("/api/v1/billing/instances", params=params).json()
        assert history_page["total"] == 1
