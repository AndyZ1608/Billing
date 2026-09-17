from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dimensions(StrictModel):
    vcpu: bool = True
    ram: bool = True
    root_disk: bool = True
    ephemeral_disk: bool = True


class NovaPolicy(StrictModel):
    counted_states: list[str]
    state_metrics: dict[str, Dimensions] = Field(default_factory=dict)


class CinderPolicy(StrictModel):
    counted_states: list[str]


class BillingPolicy(StrictModel):
    nova: NovaPolicy
    cinder: CinderPolicy


class PolicyFile(StrictModel):
    billing: BillingPolicy


def load_policy(path):
    policy = PolicyFile.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8"))).billing
    policy.nova.counted_states = [state.upper() for state in policy.nova.counted_states]
    policy.nova.state_metrics = {state.upper(): rule for state, rule in policy.nova.state_metrics.items()}
    policy.cinder.counted_states = [state.lower() for state in policy.cinder.counted_states]
    return policy
