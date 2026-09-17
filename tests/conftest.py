from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event

from app.billing.policy import load_policy
from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.models import SyncRun
from app.sync.engine import SyncManager
from tests.fakes import FakeClient


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'billing.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = make_engine(url)

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    settings = Settings(_env_file=None, database_url=url, sync_enabled=False, retain_raw_payload=True)
    sessions = make_sessions(engine)
    fake = FakeClient()
    manager = SyncManager(engine, sessions, settings, lambda: fake)
    yield SimpleNamespace(
        engine=engine,
        sessions=sessions,
        settings=settings,
        fake=fake,
        manager=manager,
        cloud_id=settings.openstack_cloud_id,
        policy=load_policy(settings.billing_policy_path),
    )
    manager.close()
    engine.dispose()


@pytest.fixture
def history(runtime, monkeypatch):
    runtime.fake.data["instances"] = runtime.fake.data["instances"][:1]
    runtime.fake.data["volumes"] = runtime.fake.data["volumes"][:1]

    def sync(at):
        monkeypatch.setattr("app.sync.engine.utcnow", lambda: at)
        run_id = runtime.manager.trigger(wait=True)
        with runtime.sessions() as db:
            assert db.get(SyncRun, run_id).status in ("SUCCESS", "PARTIAL"), db.get(SyncRun, run_id).errors
        return run_id

    runtime.sync_at = sync
    return runtime
