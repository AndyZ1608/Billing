from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Meter:
    name: str
    unit: str
    summary_key: str
    resource_type: str
    policy_key: str
    field: str | None
    divisor: int = 1


METERS = (
    Meter("compute.instance", "instance-hour", "instance_hours", "INSTANCE", "instance", None),
    Meter("compute.vcpu", "vCPU-hour", "vcpu_hours", "INSTANCE", "vcpu", "vcpus"),
    Meter("compute.ram", "GiB-hour", "ram_gib_hours", "INSTANCE", "ram", "ram_mb", 1024),
    Meter("compute.root_disk", "GiB-hour", "root_disk_gib_hours", "INSTANCE", "root_disk", "root_disk_gb"),
    Meter(
        "compute.ephemeral_disk",
        "GiB-hour",
        "ephemeral_disk_gib_hours",
        "INSTANCE",
        "ephemeral_disk",
        "ephemeral_disk_gb",
    ),
    Meter("storage.volume", "volume-hour", "volume_hours", "VOLUME", "volume", None),
    Meter("storage.volume_capacity", "GiB-hour", "volume_gib_hours", "VOLUME", "volume", "volume_size_gb"),
)
BY_NAME = {meter.name: meter for meter in METERS}


def allocations(period, policy):
    issues = []
    if period.resource_type == "INSTANCE":
        rule = policy.instance.get(period.state)
        if rule is None:
            return [], ["unknown_instance_state"]
    elif period.state not in policy.volume.counted_states:
        return [], [] if period.state in policy.volume.excluded_states else ["unknown_volume_state"]
    values = []
    for meter in METERS:
        if meter.resource_type != period.resource_type:
            continue
        if period.resource_type == "INSTANCE" and not getattr(rule, meter.policy_key):
            continue
        raw = getattr(period, meter.field) if meter.field else 1
        if raw is None:
            issues.append(f"unknown_capacity:{meter.name}")
            continue
        values.append((meter, Decimal(raw) / Decimal(meter.divisor)))
    return values, issues
