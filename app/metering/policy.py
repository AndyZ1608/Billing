import hashlib
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstanceMeters(StrictModel):
    instance: StrictBool
    vcpu: StrictBool
    ram: StrictBool
    root_disk: StrictBool
    ephemeral_disk: StrictBool


class VolumeMeters(StrictModel):
    counted_states: list[str]
    excluded_states: list[str]

    @model_validator(mode="after")
    def disjoint(self):
        if set(self.counted_states) & set(self.excluded_states):
            raise ValueError("Volume counted and excluded states must be disjoint")
        return self


class MeteringPolicy(StrictModel):
    instance: dict[str, InstanceMeters]
    volume: VolumeMeters

    @model_validator(mode="after")
    def state_case(self):
        if any(not key or key != key.upper() for key in self.instance):
            raise ValueError("Instance policy states must be uppercase")
        if any(
            not key or key != key.lower() for key in self.volume.counted_states + self.volume.excluded_states
        ):
            raise ValueError("Volume policy states must be lowercase")
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class PolicyFile(StrictModel):
    metering: MeteringPolicy


def load_metering_policy(path):
    return PolicyFile.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8"))).metering
