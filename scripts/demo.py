"""Explicit local demo using synthetic fixtures and a separate SQLite database."""

import argparse
import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    directory = Path(".local").resolve()
    directory.mkdir(exist_ok=True)
    url = f"sqlite:///{(directory / 'demo.db').as_posix()}"
    os.environ["DATABASE_URL"] = url
    os.environ["APP_ENV"] = "development"
    from app.core.config import Settings
    from app.db.session import make_engine
    from app.main import create_app
    from tests.fakes import FakeClient

    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(
        database_url=url,
        app_env="development",
        openstack_cloud_name="Synthetic demo",
        sync_enabled=True,
        sync_interval_seconds=300,
    )
    app = create_app(settings, make_engine(url), FakeClient)
    print(f"SYNTHETIC DEMO ONLY: http://127.0.0.1:{args.port} (no OpenStack connection)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
