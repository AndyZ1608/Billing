from datetime import UTC, datetime
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Uuid, select

from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.metering.math import utc
from app.models import Instance, Observation, StatePeriod
from app.openstack.normalize import normalize_instance, normalize_project
from app.sync.engine import SyncManager
from tests.fakes import FakeClient


def test_migration_preserves_phase1_and_creates_no_fabricated_backfill(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'upgrade.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "0001")
    engine = make_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    # SQLite reflects portable UUID columns as CHAR(32); restore bind conversion for this fixture.
    for table in metadata.tables.values():
        for column in table.columns:
            if column.name in {
                "cloud_id",
                "project_id",
                "instance_id",
                "volume_id",
                "sync_run_id",
                "observation_id",
                "resource_id",
            }:
                column.type = Uuid()
    cloud = UUID("00000000-0000-0000-0000-000000000001")
    old_time = datetime(2025, 1, 1, tzinfo=UTC)
    common = dict(
        first_seen_at=old_time,
        last_seen_at=old_time,
        created_at=old_time,
        updated_at=old_time,
        missing_scans=0,
        is_missing=False,
    )
    fake = FakeClient()
    old_observation = uuid4()
    run_id = uuid4()
    with engine.begin() as db:
        db.execute(
            metadata.tables["clouds"]
            .insert()
            .values(
                cloud_id=cloud,
                name="Phase 1",
                region="RegionOne",
                connection_status="CONNECTED",
                service_status={},
            )
        )
        db.execute(
            metadata.tables["projects"]
            .insert()
            .values(cloud_id=cloud, **common, **normalize_project(fake.data["projects"][0]))
        )
        db.execute(
            metadata.tables["instances"]
            .insert()
            .values(cloud_id=cloud, **common, **normalize_instance(fake.data["instances"][0], fake.flavor))
        )
        db.execute(
            metadata.tables["sync_runs"]
            .insert()
            .values(
                sync_run_id=run_id,
                cloud_id=cloud,
                started_at=old_time,
                finished_at=old_time,
                status="SUCCESS",
                projects_found=1,
                instances_found=1,
                volumes_found=0,
                errors=[],
                services={},
            )
        )
        db.execute(
            metadata.tables["resource_observations"]
            .insert()
            .values(
                observation_id=old_observation,
                cloud_id=cloud,
                sync_run_id=run_id,
                resource_type="instances",
                resource_id=UUID(fake.data["instances"][0]["id"]),
                observed_at=old_time,
                event="SEEN",
                normalized_payload={"legacy": True},
            )
        )
    command.upgrade(Config("alembic.ini"), "head")
    sessions = make_sessions(engine)
    with sessions() as db:
        assert db.get(Observation, old_observation).normalized_payload == {"legacy": True}
        assert db.scalars(select(Instance)).one().vcpus == 2
        assert list(db.scalars(select(StatePeriod))) == []
    before = datetime.now(UTC)
    settings = Settings(_env_file=None, database_url=url, sync_enabled=False)
    manager = SyncManager(engine, sessions, settings, lambda: fake)
    try:
        manager.trigger(wait=True)
        with sessions() as db:
            periods = list(db.scalars(select(StatePeriod)))
            assert periods and all(utc(p.valid_from) >= before for p in periods)
            assert all(p.history_confidence == "BASELINE" for p in periods)
            assert db.get(Observation, old_observation) is not None
    finally:
        manager.close()
        engine.dispose()
