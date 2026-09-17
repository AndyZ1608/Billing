import copy
import json
from pathlib import Path


class FakeClient:
    def __init__(self):
        self.data = json.loads((Path(__file__).parent / "fixtures/inventory.json").read_text())
        self.failures = {}
        self.auth_error = None
        self.block = None

    def authenticate(self):
        if self.auth_error:
            raise self.auth_error
        if self.block:
            self.block.wait(timeout=10)

    def collect(self, kind):
        # Yield a page first, then fail: tests cover late pagination failures.
        yield from copy.deepcopy(self.data[kind])
        if kind in self.failures:
            raise self.failures[kind]

    def projects(self):
        return self.collect("projects")

    def instances(self):
        return self.collect("instances")

    def volumes(self):
        return self.collect("volumes")

    def flavor(self, key):
        raise KeyError(key)

    def close(self):
        pass
