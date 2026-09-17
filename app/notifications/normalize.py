"""Allowlisted Nova versioned payload adapter. Never infer state from action names."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

STATES = {
    "active": "ACTIVE",
    "stopped": "SHUTOFF",
    "paused": "PAUSED",
    "suspended": "SUSPENDED",
    "rescued": "RESCUE",
    "shelved": "SHELVED",
    "shelved_offloaded": "SHELVED_OFFLOADED",
    "error": "ERROR",
    "building": "BUILD",
    "deleted": "DELETED",
    "soft_deleted": "SOFT_DELETED",
    "resized": "VERIFY_RESIZE",
}


class InvalidNotification(ValueError):
    pass


def unwrap(value):
    if not isinstance(value, dict):
        raise InvalidNotification("INVALID_VERSIONED_PAYLOAD")
    if value.get("nova_object.namespace") != "nova" or not str(
        value.get("nova_object.version", "")
    ).startswith("1."):
        raise InvalidNotification("UNSUPPORTED_PAYLOAD_VERSION")
    data = value.get("nova_object.data")
    if not isinstance(data, dict):
        raise InvalidNotification("INVALID_VERSIONED_PAYLOAD")
    return data


def envelope(body):
    if isinstance(body, (str, bytes)):
        body = json.loads(body)
    if isinstance(body, dict) and "oslo.message" in body:
        body = json.loads(body["oslo.message"])
    if not isinstance(body, dict):
        raise InvalidNotification("INVALID_ENVELOPE")
    return body


def identity(body):
    # Hash even supplied IDs to bound length and avoid storing arbitrary header text.
    stable = body.get("message_id") if isinstance(body, dict) else None
    return hashlib.sha256(
        (str(stable) if stable else json.dumps(body, sort_keys=True, default=str)).encode()
    ).hexdigest()


def normalize(body):
    kind = body.get("event_type", "")
    if not isinstance(kind, str) or not kind.startswith("instance."):
        return None
    if kind != "instance.update" and not kind.endswith(".end"):
        return None  # .start/error notifications are not a completed state transition
    data = unwrap(body.get("payload"))
    transition = unwrap(data["state_update"]) if data.get("state_update") else {}
    state = transition.get("state", data.get("state"))
    if not isinstance(state, str) or not state or len(state) > 64:
        raise InvalidNotification("MISSING_VM_STATE")
    stamp = body.get("timestamp")
    if not isinstance(stamp, str):
        raise InvalidNotification("MISSING_EVENT_TIMESTAMP")
    # oslo.messaging's envelope convention is UTC even when serialized without an offset.
    timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    values = {}
    if data.get("flavor"):
        flavor = unwrap(data["flavor"])
        for source, target in (
            ("vcpus", "vcpus"),
            ("memory_mb", "ram_mb"),
            ("root_gb", "root_disk_gb"),
            ("ephemeral_gb", "ephemeral_disk_gb"),
        ):
            value = flavor.get(source)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2147483647:
                    raise InvalidNotification("INVALID_ALLOCATION")
                values[target] = value
        if isinstance(flavor.get("name"), str):
            values["flavor_name"] = flavor["name"][:255]
        if flavor.get("flavorid") is not None:
            values["flavor_id"] = str(flavor["flavorid"])[:255]
    return dict(
        source="nova_notification",
        event_id=identity(body),
        event_type=kind[:128],
        timestamp=timestamp,
        instance_id=UUID(data["uuid"]),
        project_id=UUID(data["tenant_id"]),
        old_state=STATES.get(transition.get("old_state"), str(transition.get("old_state", "")).upper())
        or None,
        new_state=STATES.get(state, state.upper()),
        allocations=values,
    )
