"""The shared per-cloud lock used by pricing and rating, matching sync/metering."""

import hashlib
import threading
from contextlib import contextmanager

from sqlalchemy import text


class JobBusy(Exception):
    pass


class CloudLock:
    def __init__(self, engine, cloud_id, gate=None):
        self.engine = engine
        self.gate = gate if gate is not None else threading.Lock()
        self.key = int.from_bytes(hashlib.sha256(str(cloud_id).encode()).digest()[:8], "big", signed=True)

    @contextmanager
    def held(self):
        if not self.gate.acquire(blocking=False):
            raise JobBusy("Another cloud job or pricing edit is running")
        connection = None
        acquired = False
        try:
            if self.engine.dialect.name == "postgresql":
                connection = self.engine.connect()
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key})
                connection.commit()
                if not acquired:
                    raise JobBusy("Another cloud job or pricing edit is running")
            yield
        finally:
            if connection:
                try:
                    if acquired:
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
                        connection.commit()
                except Exception:
                    connection.invalidate()
                finally:
                    connection.close()
            self.gate.release()
