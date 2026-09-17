from datetime import timedelta

from app.core.jobs import CloudLock
from app.core.logging import event
from app.lifecycle.engine import LifecycleBatch, safe_snapshot
from app.metering.math import utc
from app.metering.policy import load_metering_policy
from app.models import Instance, LifecycleHead, ProcessedNotification, StatePeriod, utcnow
from app.notifications.normalize import InvalidNotification, envelope, identity, normalize


class NotificationProcessor:
    def __init__(self, engine, sessions, settings, gate=None):
        self.sessions, self.settings = sessions, settings
        self.cloud = settings.openstack_cloud_id
        self.known_states = set(load_metering_policy(settings.metering_policy_path).instance)
        self.lock = CloudLock(engine, self.cloud, gate)

    def process(self, body, received_at=None):
        received = utc(received_at or utcnow())
        try:
            body = envelope(body)
            key = identity(body)
            normalized = normalize(body)
            issue = None
        except (ValueError, KeyError, TypeError) as exc:
            key, normalized = identity(body), None
            issue = str(exc) if isinstance(exc, InvalidNotification) else "INVALID_NOTIFICATION"
        with self.lock.held(), self.sessions.begin() as db:
            existing = db.get(ProcessedNotification, (self.cloud, key))
            if existing:
                return {"status": "DUPLICATE", "reconcile": existing.status == "QUARANTINED"}
            row = ProcessedNotification(
                cloud_id=self.cloud,
                event_id=key,
                received_at=received,
                event_type=normalized["event_type"] if normalized else "ignored_or_invalid",
                status="QUARANTINED" if issue else "IGNORED",
                issue_code=issue,
                normalized_event=safe_snapshot(normalized or {}),
            )
            db.add(row)
            if normalized:
                row.instance_id, row.project_id = normalized["instance_id"], normalized["project_id"]
                row.event_timestamp = at = normalized["timestamp"]
                vm = db.get(Instance, (self.cloud, row.instance_id))
                head = db.get(LifecycleHead, (self.cloud, "INSTANCE", row.instance_id))
                period = db.get(StatePeriod, head.latest_period_id) if head else None
                if at > received + timedelta(seconds=self.settings.nova_notification_max_future_seconds):
                    issue = "FUTURE_EVENT_TIMESTAMP"
                elif at > received:
                    issue = "CLOCK_SKEW"  # do not create future usage even within tolerance
                elif vm is None or head is None:
                    issue = "MISSING_INVENTORY_BASELINE"
                elif vm.project_id != row.project_id:
                    issue = "PROJECT_OWNERSHIP_CONFLICT"
                elif at < utc(head.last_observed_at) or at <= utc(period.valid_from):
                    issue = "OUT_OF_ORDER_EVENT"
                elif normalized["old_state"] and normalized["old_state"] != vm.status:
                    issue = "STATE_CHAIN_GAP"
                elif period.valid_to is not None:
                    issue = "CLOSED_RESOURCE_CONFLICT"
                if issue:
                    row.status, row.issue_code = "QUARANTINED", issue
                else:
                    values = {
                        k: getattr(vm, k)
                        for k in (
                            "project_id",
                            "instance_name",
                            "status",
                            "flavor_id",
                            "flavor_name",
                            "vcpus",
                            "ram_mb",
                            "root_disk_gb",
                            "ephemeral_disk_gb",
                            "boot_source",
                            "created_at_openstack",
                            "updated_at_openstack",
                            "deleted_at_openstack",
                            "quality_issues",
                        )
                    }
                    changed_flavor = normalized["allocations"].get("flavor_id", vm.flavor_id) != vm.flavor_id
                    if changed_flavor:
                        values["flavor_name"] = None
                        for field in ("vcpus", "ram_mb", "root_disk_gb", "ephemeral_disk_gb"):
                            values[field] = None
                    values.update(normalized["allocations"])
                    if vm.boot_source == "volume":
                        values["root_disk_gb"] = 0
                    elif vm.boot_source != "image" or values["root_disk_gb"] == 0:
                        values["root_disk_gb"] = None
                    values["status"] = normalized["new_state"]
                    values["updated_at_openstack"] = at
                    values["quality_issues"] = list(vm.quality_issues)
                    if values["status"] not in self.known_states:
                        values["quality_issues"].append("unknown_instance_state")
                        row.issue_code = "UNKNOWN_VM_STATE"
                    if any(
                        values[k] is None for k in ("vcpus", "ram_mb", "root_disk_gb", "ephemeral_disk_gb")
                    ):
                        values["quality_issues"].append("notification_missing_allocation")
                        row.issue_code = "MISSING_ALLOCATION"
                    if values["status"] in ("DELETED", "SOFT_DELETED"):
                        values["deleted_at_openstack"] = at
                        vm.deleted_confirmed_at = received
                    lifecycle = LifecycleBatch(db, self.cloud, "instances", None, at, received_at=received)
                    lifecycle.seen(vm.instance_id, values)
                    for k, v in values.items():
                        setattr(vm, k, v)
                    vm.last_seen_at, vm.updated_at = at, received
                    vm.missing_scans, vm.missing_since, vm.is_missing = 0, None, False
                    row.status = "APPLIED"
            event(
                "NOVA_NOTIFICATION_" + row.status,
                cloud_id=self.cloud,
                event_id=key,
                instance_id=row.instance_id,
                code=row.issue_code,
            )
            return {"status": row.status, "reconcile": row.status == "QUARANTINED" or bool(row.issue_code)}
