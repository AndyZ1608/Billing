from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.models import Base, Instance, Project


def test_api_end_to_end_and_input_validation(runtime):
    app = create_app(runtime.settings, runtime.engine, lambda: runtime.fake)
    with TestClient(app) as client:
        app.state.sync_manager.trigger(wait=True)
        assert client.get("/api/v1/readiness").json()["ready"]
        assert client.get("/api/v1/health").json()["openstack"] == "CONNECTED"
        assert client.get("/api/v1/billing/current").json()["vcpu_count"] == 14
        page = client.get(
            "/api/v1/billing/projects", params={"sort": "vcpu_count", "direction": "desc"}
        ).json()
        assert page["items"][0]["project_name"] == "Project B"
        pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert client.get("/api/v1/projects", params={"q": "Project A"}).json()["total"] == 1
        assert client.get("/api/v1/billing/projects", params={"q": pid}).json()["total"] == 1
        vm_page = client.get(f"/api/v1/projects/{pid}/instances?limit=1").json()
        assert vm_page["total"] == 2 and len(vm_page["items"]) == 1
        assert vm_page["items"][0]["last_seen_at"].endswith("+00:00")
        assert "raw_payload" not in vm_page["items"][0]
        assert client.get("/api/v1/projects/not-a-uuid").status_code == 422
        assert client.get(f"/api/v1/projects/{uuid4()}/volumes").status_code == 404
        assert client.get("/api/v1/billing/projects?sort=DROP+TABLE").status_code == 422
        assert client.get("/api/v1/projects?limit=10000").status_code == 422
        assert client.post("/api/v1/sync").status_code == 415
        assert client.post("/api/v1/sync", json={}).status_code == 202
        app.state.sync_manager.future.result(timeout=10)
        assert "Current allocated" in client.get("/").text
        assert client.get(f"/projects/{pid}").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        for path in ("/api/v1/cloud", "/api/v1/health", "/api/v1/sync-runs"):
            response = client.get(path).text.lower()
            assert "os_password" not in response
            assert "application_credential_secret" not in response


def test_migration_matches_models_and_is_reversible(runtime):
    with runtime.engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert not diff
    command.downgrade(Config("alembic.ini"), "base")
    command.upgrade(Config("alembic.ini"), "head")
    runtime.manager.trigger(wait=True)


def test_uuid_uniqueness_and_foreign_keys(runtime):
    runtime.manager.trigger(wait=True)
    with pytest.raises(IntegrityError), runtime.sessions.begin() as db:
        db.add(Project(cloud_id=runtime.cloud_id, project_id=UUID("a" * 32), project_name="Duplicate"))
        db.flush()
    with pytest.raises(IntegrityError), runtime.sessions.begin() as db:
        instance = db.scalars(select(Instance)).first()
        instance.project_id = uuid4()
        db.flush()


def test_cloud_isolation(runtime):
    runtime.manager.trigger(wait=True)
    from app.billing.aggregate import project_summaries

    with runtime.sessions() as db:
        assert project_summaries(db, uuid4(), runtime.policy) == []
