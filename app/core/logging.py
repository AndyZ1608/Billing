import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Only our structured, explicitly selected fields are emitted. Exception
        # messages/tracebacks may contain SDK response bodies or connection URLs.
        payload = {"time": datetime.now(UTC).isoformat(), "level": record.levelname}
        payload.update(getattr(record, "event_data", {"event": "application_message"}))
        return json.dumps(payload, default=str)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("billing")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Suppress third-party HTTP/SQL debug logs, including token-bearing payloads.
    for name in ("openstack", "keystoneauth", "keystoneauth1", "urllib3", "requests", "sqlalchemy"):
        external = logging.getLogger(name)
        external.handlers = [logging.NullHandler()]
        external.propagate = False
        external.setLevel(logging.CRITICAL + 1)


def event(name: str, **fields):
    logging.getLogger("billing").info(name, extra={"event_data": {"event": name, **fields}})
