"""Synthetic Phase 2 walkthrough; never connects to OpenStack or rewrites existing history."""

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument(
        "--pricing-demo", action="store_true", help="Separate cost-demo.db with TEST POC-VND prices"
    )
    parser.add_argument(
        "--billing-demo",
        action="store_true",
        help="Separate billing-demo.db with synthetic prices and local demo admin token",
    )
    parser.add_argument("--internal-demo", action="store_true")
    args = parser.parse_args()
    if args.billing_demo:
        os.environ["BILLING_ADMIN_TOKEN"] = "synthetic-billing-demo-only"

    directory = Path(".local").resolve()
    directory.mkdir(exist_ok=True)
    filename = (
        "internal-demo.db"
        if args.internal_demo
        else "billing-phase4-demo.db"
        if args.billing_demo
        else "cost-demo.db"
        if args.pricing_demo
        else "history-demo.db"
    )
    url = f"sqlite:///{(directory / filename).as_posix()}"
    os.environ["DATABASE_URL"], os.environ["APP_ENV"] = url, "development"
    from app.core.config import Settings
    from app.db.session import make_engine, make_sessions
    from app.main import create_app
    from app.metering.engine import MeteringEngine
    from app.models import StatePeriod
    from app.sync.engine import SyncManager
    from tests.fakes import FakeClient

    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(
        database_url=url, app_env="development", openstack_cloud_name="Synthetic lifecycle demo"
    )
    engine = make_engine(url)
    sessions = make_sessions(engine)
    fake = FakeClient()
    with sessions() as db:
        seeded = db.scalar(select(func.count()).select_from(StatePeriod)) > 0
    if not seeded:
        point = [datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)]
        manager = SyncManager(engine, sessions, settings, lambda: fake, clock=lambda: point[0])
        try:
            fake.data["instances"][0]["flavor"].update(vcpus=4, ram=8192, disk=50)
            manager.trigger(wait=True)
            point[0] += timedelta(hours=2)
            fake.data["instances"][0]["flavor"].update(id="resized", vcpus=8, ram=16384)
            manager.trigger(wait=True)
            point[0] += timedelta(hours=3)
            fake.data["instances"][0]["status"] = "SHUTOFF"
            fake.data["volumes"][0]["size"] = 200
            manager.trigger(wait=True)
            if args.billing_demo:
                point[0] += timedelta(hours=1)
                for instance in fake.data["instances"]:
                    instance["status"] = "ERROR"
                for volume in fake.data["volumes"]:
                    volume["status"] = "error"
                manager.trigger(wait=True)
            MeteringEngine(engine, sessions, settings).run()
        finally:
            manager.close()
    # The live synthetic collector continues the final seeded configuration.
    fake.data["instances"][0]["flavor"].update(id="resized", vcpus=8, ram=16384, disk=50)
    fake.data["instances"][0]["status"] = "SHUTOFF"
    fake.data["volumes"][0]["size"] = 200
    if args.billing_demo:
        for instance in fake.data["instances"]:
            instance["status"] = "ERROR"
        for volume in fake.data["volumes"]:
            volume["status"] = "error"
    if args.pricing_demo or args.billing_demo:
        from app.pricing.bootstrap import seed_demo
        from app.rating.engine import RatingEngine

        with sessions.begin() as db:
            seed_demo(db, settings.openstack_cloud_id, "synthetic-demo")
        RatingEngine(engine, sessions, settings).run()
    if args.internal_demo:
        from app.metering.math import utc
        from app.pricing.internal import seed_internal
        from app.rating.engine import RatingEngine

        with sessions.begin() as db:
            first = db.scalar(select(func.min(StatePeriod.valid_from)))
            seed_internal(db, settings, utc(first), "synthetic-demo")
        RatingEngine(engine, sessions, settings).run()
    page = (
        ""
        if args.internal_demo
        else "billing-cycles"
        if args.billing_demo
        else "costs"
        if args.pricing_demo
        else "history"
    )
    print(f"SYNTHETIC DEMO: http://127.0.0.1:{args.port}/{page}")
    uvicorn.run(
        create_app(settings, engine, lambda: fake), host="127.0.0.1", port=args.port, access_log=False
    )


if __name__ == "__main__":
    main()
