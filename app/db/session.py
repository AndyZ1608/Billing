from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(url: str):
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    else:
        # Keep project/resource reads consistent within a request during a sync.
        options["isolation_level"] = "REPEATABLE READ"
        options["connect_args"] = {"connect_timeout": 10}
    engine = create_engine(url, **options)
    if not url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def utc_session(connection, record):
            with connection.cursor() as cursor:
                cursor.execute("SET TIME ZONE 'UTC'")
            connection.commit()

    return engine


def make_sessions(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
