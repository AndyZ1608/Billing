"""Operations against the configured database; schema changes remain Alembic's job."""

import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import make_engine, make_sessions
from app.metering.engine import MeteringEngine
from app.metering.math import utc
from app.models import StatePeriod, SyncRun
from app.pricing.bootstrap import seed_demo
from app.rating.engine import RatingEngine
from app.sync.engine import SyncManager, ensure_cloud


def aware(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp requires an offset, e.g. 2026-09-01T00:00:00Z")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="domain", required=True)
    sync = actions.add_parser("sync")
    sync.add_argument("action", choices=["run"])
    pricing = actions.add_parser("pricing")
    pricing.add_argument(
        "action", choices=["seed-demo", "seed-internal"], help="Explicit TEST POC-VND bootstrap"
    )
    pricing.add_argument(
        "--from", dest="start", type=aware, help="Required effective start for internal rates"
    )
    rating = actions.add_parser("rating")
    rating.add_argument("action", choices=["run"])
    rating.add_argument("--from", dest="start", type=aware)
    rating.add_argument("--to", dest="end", type=aware)
    rating.add_argument("--force", action="store_true", help="ADMIN: supersede selected usage charge sets")
    rating.add_argument("--actor", default="cli-admin")
    metering = actions.add_parser("metering")
    metering.add_argument("action", choices=["run"])
    metering.add_argument("--from", dest="start", type=aware)
    metering.add_argument("--to", dest="end", type=aware)
    metering.add_argument(
        "--force", action="store_true", help="Idempotent canonical replay, never deletes records"
    )
    lifecycle = actions.add_parser("lifecycle")
    lifecycle.add_argument("action", choices=["inspect"])
    lifecycle.add_argument("resource_type", choices=["INSTANCE", "VOLUME"])
    lifecycle.add_argument("resource_id", type=UUID)
    args = parser.parse_args()
    configure_logging()
    settings = Settings()
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = make_sessions(engine)
    manager = None
    try:
        with sessions.begin() as db:
            ensure_cloud(db, settings)
        if args.domain == "pricing":
            rating_engine = RatingEngine(engine, sessions, settings)
            with rating_engine.lock.held(), sessions.begin() as db:
                if args.action == "seed-internal":
                    from app.pricing.internal import seed_internal

                    if args.start is None:
                        parser.error(
                            "pricing seed-internal requires --from with a timezone-aware effective date"
                        )
                    run = seed_internal(db, settings, args.start)
                else:
                    run = seed_demo(db, settings.openstack_cloud_id, "cli-bootstrap")
            print(json.dumps({c.key: getattr(run, c.key) for c in run.__table__.columns}, default=str))
            return 0
        elif args.domain == "rating":
            from app.api.metering import RunRequest

            RunRequest(start=args.start, end=args.end, force=args.force)
            run = RatingEngine(engine, sessions, settings).run(args.start, args.end, args.force, args.actor)
        elif args.domain == "sync":
            manager = SyncManager(engine, sessions, settings)
            run_id = manager.trigger(wait=True)
            with sessions() as db:
                run = db.get(SyncRun, run_id)
        elif args.domain == "metering":
            from app.api.metering import RunRequest

            RunRequest(start=args.start, end=args.end, force=args.force)
            run = MeteringEngine(engine, sessions, settings).run(args.start, args.end, args.force)
        else:
            with sessions() as db:
                rows = db.scalars(
                    select(StatePeriod)
                    .where(
                        StatePeriod.cloud_id == settings.openstack_cloud_id,
                        StatePeriod.resource_type == args.resource_type,
                        StatePeriod.resource_id == args.resource_id,
                    )
                    .order_by(StatePeriod.valid_from)
                )
                print(
                    json.dumps(
                        [{c.key: getattr(row, c.key) for c in row.__table__.columns} for row in rows],
                        default=lambda v: utc(v).isoformat() if isinstance(v, datetime) else str(v),
                    )
                )
            return 0
        print(
            json.dumps(
                {c.key: getattr(run, c.key) for c in run.__table__.columns},
                default=lambda v: utc(v).isoformat() if isinstance(v, datetime) else str(v),
            )
        )
        return 1 if run.status == "FAILED" else 0
    finally:
        if manager:
            manager.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
