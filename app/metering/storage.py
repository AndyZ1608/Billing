"""Cinder capacity follows the union of attached VMs' ACTIVE lifecycle intervals."""

from uuid import UUID

from sqlalchemy import or_, select

from app.metering.math import utc
from app.models import LifecycleHead, Observation, StatePeriod


def billable_windows(db, period, policy, lower, upper):
    lower, upper = utc(lower), utc(upper)
    if lower >= upper:
        return [], [], True
    if period.resource_type != "VOLUME" or not policy.volume.active_attachment_only:
        return [(lower, upper)], [], True
    observation = db.get(Observation, period.source_observation_id)
    if not observation or "attachments" not in observation.normalized_payload:
        return [], ["unknown_volume_attachments"], False
    owners = {UUID(a["instance_id"]) for a in observation.normalized_payload["attachments"]}
    if not owners:
        return [], [], True
    periods = list(
        db.scalars(
            select(StatePeriod).where(
                StatePeriod.cloud_id == period.cloud_id,
                StatePeriod.project_id == period.project_id,
                StatePeriod.resource_type == "INSTANCE",
                StatePeriod.resource_id.in_(owners),
                StatePeriod.valid_from < upper,
                or_(StatePeriod.valid_to.is_(None), StatePeriod.valid_to > lower),
            )
        )
    )
    known = {p.resource_id for p in periods}
    issues = ["unknown_attached_vm_history"] if known != owners else []
    stable = not issues
    windows = []
    for vm in periods:
        if vm.valid_to is None:
            head = db.get(LifecycleHead, (period.cloud_id, "INSTANCE", vm.resource_id))
            stable &= head is not None and utc(head.last_observed_at) >= upper
        if vm.state not in policy.instance:
            issues.append("unknown_attached_vm_state")
        if vm.state == "ACTIVE":
            windows.append(
                (max(lower, utc(vm.valid_from)), min(upper, utc(vm.valid_to)) if vm.valid_to else upper)
            )
    # Union, not sum: multiattached capacity is billed once.
    merged = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged, issues, stable
