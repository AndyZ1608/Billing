import hashlib
import json
from uuid import uuid4

from sqlalchemy import select

from app.core.logging import event
from app.metering.math import utc
from app.models import LifecycleHead, Observation, StatePeriod


class LifecycleError(ValueError):
    pass


def safe_snapshot(values):
    return json.loads(json.dumps(values, default=str))


def billable_hash(kind, values):
    keys = (
        "project_id",
        "status",
        "flavor_id",
        "vcpus",
        "ram_mb",
        "root_disk_gb",
        "ephemeral_disk_gb",
        "boot_source",
    )
    if kind == "volumes":
        keys = ("project_id", "status", "size_gb", "volume_type", "attachments")
    data = {key: values.get(key) for key in keys}
    return hashlib.sha256(
        json.dumps(safe_snapshot(data), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def retain_known_allocation(resource, values):
    if resource is None or values.get("flavor_id") is None or resource.flavor_id != values["flavor_id"]:
        return
    for field in ("vcpus", "ram_mb", "root_disk_gb", "ephemeral_disk_gb"):
        if field == "root_disk_gb" and resource.boot_source != values["boot_source"]:
            continue
        if values[field] is None and getattr(resource, field) is not None:
            values[field] = getattr(resource, field)
            values["quality_issues"].append(f"retained_known_{field}")


class LifecycleBatch:
    """Indexed mutable heads + immutable periods; runs inside the inventory transaction."""

    def __init__(self, db, cloud_id, kind, run_id, now, received_at=None):
        self.db, self.cloud_id, self.kind, self.run_id, self.now = db, cloud_id, kind, run_id, utc(now)
        self.received_at = utc(received_at) if received_at else self.now
        self.resource_type = "INSTANCE" if kind == "instances" else "VOLUME"
        pairs = db.execute(
            select(LifecycleHead, StatePeriod)
            .join(StatePeriod, LifecycleHead.latest_period_id == StatePeriod.period_id)
            .where(LifecycleHead.cloud_id == cloud_id, LifecycleHead.resource_type == self.resource_type)
        )
        self.heads = {head.resource_id: (head, period) for head, period in pairs}

    def log(self, name, resource_id, project_id, **extra):
        event(
            name,
            cloud_id=self.cloud_id,
            project_id=project_id,
            resource_type=self.resource_type,
            resource_id=resource_id,
            sync_run_id=self.run_id,
            **extra,
        )

    def observation(self, resource_id, project_id, values, kind="SEEN", fingerprint=None):
        observation = Observation(
            observation_id=uuid4(),
            cloud_id=self.cloud_id,
            sync_run_id=self.run_id,
            resource_type=self.kind,
            resource_id=resource_id,
            project_id=project_id,
            observed_at=self.now,
            event=kind,
            payload_hash=fingerprint,
            normalized_payload=safe_snapshot(values),
        )
        self.db.add(observation)
        self.db.flush()
        return observation

    def close(self, period, end, observation, reason, first_missing=None, confirmed=None):
        end = utc(end)
        if period.valid_to is not None or end <= utc(period.valid_from) or end > self.now:
            raise LifecycleError("invalid_lifecycle_closure")
        period.valid_to, period.closed_at = end, self.received_at
        period.closing_observation_id, period.closure_reason = observation.observation_id, reason
        period.first_missing_at, period.deletion_confirmed_at = first_missing, confirmed
        self.db.flush()  # release the one-open-period constraint before inserting a successor

    def seen(self, resource_id, values, was_pending=False):
        fingerprint = billable_hash(self.kind, values)
        prior = self.heads.get(resource_id)
        explicit_deleted = self.kind == "instances" and (
            values.get("deleted_at_openstack") is not None or values["status"] in ("DELETED", "SOFT_DELETED")
        )
        if prior:
            head, period = prior
            if self.now < utc(head.last_observed_at):
                raise LifecycleError("out_of_order_observation")
            if period.valid_to is None and fingerprint == head.last_payload_hash and not explicit_deleted:
                head.last_observed_at = self.now
                if was_pending:
                    self.observation(resource_id, values["project_id"], values, "REAPPEARED", fingerprint)
                    self.log("RESOURCE_REAPPEARED", resource_id, values["project_id"])
                return
            if period.valid_to is not None and explicit_deleted:
                head.last_observed_at = self.now
                return
        observation = self.observation(resource_id, values["project_id"], values, fingerprint=fingerprint)
        if prior:
            head, period = prior
            if period.valid_to is None:
                end, reason = self.now, "STATE_OR_ALLOCATION_CHANGE"
                if explicit_deleted:
                    supplied = values.get("deleted_at_openstack")
                    if (
                        supplied
                        and utc(period.valid_from) < utc(supplied)
                        and utc(head.last_observed_at) <= utc(supplied) <= self.now
                    ):
                        end = supplied
                    reason = "OPENSTACK_DELETION" if supplied and end == supplied else "OBSERVED_DELETION"
                self.close(period, end, observation, reason, confirmed=self.now if explicit_deleted else None)
            elif self.now < utc(period.valid_to):
                raise LifecycleError("overlapping_reappearance")
        if explicit_deleted:
            self.log("RESOURCE_DELETION_CONFIRMED", resource_id, values["project_id"], evidence="OPENSTACK")
            return
        period = StatePeriod(
            period_id=uuid4(),
            cloud_id=self.cloud_id,
            project_id=values["project_id"],
            resource_type=self.resource_type,
            resource_id=resource_id,
            resource_name=values.get("instance_name", values.get("volume_name", "Unnamed")),
            valid_from=self.now,
            state=values["status"],
            payload_hash=fingerprint,
            flavor_id=values.get("flavor_id"),
            flavor_name=values.get("flavor_name"),
            vcpus=values.get("vcpus"),
            ram_mb=values.get("ram_mb"),
            root_disk_gb=values.get("root_disk_gb"),
            ephemeral_disk_gb=values.get("ephemeral_disk_gb"),
            boot_source=values.get("boot_source"),
            volume_size_gb=values.get("size_gb"),
            volume_type=values.get("volume_type"),
            openstack_created_at=values.get("created_at_openstack"),
            openstack_updated_at=values.get("updated_at_openstack"),
            history_confidence="BASELINE" if prior is None else "OBSERVED",
            effective_time_source="NOVA_NOTIFICATION" if self.run_id is None else "OBSERVATION",
            quality_issues=values.get("quality_issues", []),
            source_observation_id=observation.observation_id,
            created_at=self.now,
        )
        self.db.add(period)
        self.db.flush()
        if prior:
            head = prior[0]
            head.latest_period_id, head.last_payload_hash, head.last_observed_at = (
                period.period_id,
                fingerprint,
                self.now,
            )
        else:
            head = LifecycleHead(
                cloud_id=self.cloud_id,
                resource_type=self.resource_type,
                resource_id=resource_id,
                latest_period_id=period.period_id,
                last_observed_at=self.now,
                last_payload_hash=fingerprint,
            )
            self.db.add(head)
        self.heads[resource_id] = (head, period)
        self.log(
            "LIFECYCLE_CREATED" if prior is None else "LIFECYCLE_CHANGED",
            resource_id,
            values["project_id"],
            source_state_period_id=period.period_id,
        )

    def missing(self, resource_id, resource):
        if resource.deleted_confirmed_at is not None:
            return  # confirmed absence needs no identical audit event on every later poll
        prior = self.heads.get(resource_id)
        confirmed = resource.is_missing and resource.deleted_confirmed_at is None
        if confirmed:
            resource.deleted_confirmed_at = self.now
        observation = self.observation(
            resource_id,
            resource.project_id,
            {
                "missing_scans": resource.missing_scans,
                "is_missing": resource.is_missing,
                "first_missing_at": resource.missing_since,
                "deletion_confirmed_at": resource.deleted_confirmed_at,
            },
            "MISSING",
        )
        self.log("RESOURCE_MISSING", resource_id, resource.project_id, missing_scans=resource.missing_scans)
        if confirmed and prior and prior[1].valid_to is None:
            self.close(
                prior[1],
                resource.missing_since,
                observation,
                "CONFIRMED_DISAPPEARANCE",
                first_missing=resource.missing_since,
                confirmed=self.now,
            )
            self.log(
                "RESOURCE_DELETION_CONFIRMED",
                resource_id,
                resource.project_id,
                first_missing_at=resource.missing_since,
                deletion_confirmed_at=self.now,
            )
