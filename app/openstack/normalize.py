from datetime import UTC, datetime
from uuid import UUID


class MalformedResource(ValueError):
    pass


def payload(resource):
    return resource.to_dict() if hasattr(resource, "to_dict") else dict(resource)


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise MalformedResource("invalid_resource_id") from None


def optional_text(value, limit=255):
    return str(value)[:limit] if value is not None else None


def timestamp(value, issues):
    if not value:
        return None
    try:
        dt = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, TypeError):
        issues.append("invalid_timestamp")
        return None


def boolean(value):
    if value is True or str(value).lower() in ("true", "1"):
        return True
    if value is False or str(value).lower() in ("false", "0"):
        return False
    return None


def quantity(value, field, issues):
    try:
        # Reject fractional, negative, boolean or unbounded values; unknown != zero.
        if (
            isinstance(value, bool)
            or str(value).strip() != str(int(value))
            or not 0 <= int(value) <= 2147483647
        ):
            raise ValueError()
        return int(value)
    except (ValueError, TypeError, OverflowError):
        issues.append(f"unknown_{field}")
        return None


def project_identifier(values, *keys):
    identifiers = {identifier(values[key]) for key in keys if values.get(key)}
    if len(identifiers) != 1:
        raise MalformedResource("missing_or_conflicting_project_id")
    return identifiers.pop()


def normalize_project(resource):
    p = payload(resource)
    return {
        "project_id": identifier(p.get("id")),
        "project_name": optional_text(p.get("name")) or "Unnamed",
        "domain_id": optional_text(p.get("domain_id")),
        "enabled": boolean(p.get("is_enabled", p.get("enabled"))),
        "is_placeholder": False,
    }


def normalize_instance(resource, flavor_lookup):
    p = payload(resource)
    issues = []
    flavor = p.get("flavor") or {}
    if not isinstance(flavor, dict):
        flavor = {"id": str(flavor)}
    flavor_id = flavor.get("id") or p.get("flavor_id")
    if not all(flavor.get(key) is not None for key in ("vcpus", "ram", "disk", "ephemeral")) and flavor_id:
        try:
            fallback = payload(flavor_lookup(str(flavor_id)))
            flavor = {**fallback, **{key: value for key, value in flavor.items() if value is not None}}
        except Exception:
            # Includes deleted/private flavor (404/403). Keep VM, flag unknown dimensions.
            issues.append("flavor_unavailable")
    image = p.get("image")
    if isinstance(image, dict) and image.get("id") or isinstance(image, str) and image:
        boot_source = "image"
    elif image == "" or image == {}:
        # Nova server-detail explicitly returns an empty image for volume boot.
        boot_source = "volume"
    else:
        boot_source = "unknown"
        issues.append("unknown_boot_source")
    root = quantity(flavor.get("disk"), "root_disk", issues) if boot_source == "image" else None
    if boot_source == "volume":
        root = 0
    elif boot_source == "image" and root == 0:
        # A zero-disk flavor can use the image's size, which is not in this inventory.
        root = None
        issues.append("image_sized_root_disk")
    return {
        "instance_id": identifier(p.get("id")),
        "project_id": project_identifier(p, "project_id", "tenant_id", "tenantId"),
        "instance_name": optional_text(p.get("name")) or "Unnamed",
        "status": (optional_text(p.get("status"), 64) or "UNKNOWN").upper(),
        "created_at_openstack": timestamp(p.get("created_at") or p.get("created"), issues),
        "updated_at_openstack": timestamp(p.get("updated_at") or p.get("updated"), issues),
        "deleted_at_openstack": timestamp(p.get("terminated_at") or p.get("deleted_at"), issues),
        "flavor_id": optional_text(flavor_id),
        "flavor_name": optional_text(flavor.get("original_name") or flavor.get("name")),
        "vcpus": quantity(flavor.get("vcpus"), "vcpus", issues),
        "ram_mb": quantity(flavor.get("ram"), "ram", issues),
        "root_disk_gb": root,
        "ephemeral_disk_gb": quantity(
            flavor.get("ephemeral", flavor.get("OS-FLV-EXT-DATA:ephemeral")), "ephemeral_disk", issues
        ),
        "boot_source": boot_source,
        "host": optional_text(p.get("compute_host") or p.get("OS-EXT-SRV-ATTR:host") or p.get("host")),
        "availability_zone": optional_text(
            p.get("availability_zone") or p.get("OS-EXT-AZ:availability_zone")
        ),
        "quality_issues": issues,
    }


def normalize_volume(resource):
    p = payload(resource)
    issues = []
    attachments = []
    for attachment in p.get("attachments") or []:
        try:
            attachments.append(
                {
                    "instance_id": str(identifier(attachment.get("server_id"))),
                    "device": optional_text(attachment.get("device")),
                }
            )
        except (MalformedResource, AttributeError):
            issues.append("invalid_attachment")
    return {
        "volume_id": identifier(p.get("id")),
        "project_id": project_identifier(p, "project_id", "os-vol-tenant-attr:tenant_id"),
        "volume_name": optional_text(p.get("name")) or "Unnamed",
        "status": (optional_text(p.get("status"), 64) or "unknown").lower(),
        "volume_type": optional_text(p.get("volume_type")),
        "size_gb": quantity(p.get("size"), "size", issues),
        "bootable": boolean(p.get("is_bootable", p.get("bootable"))),
        "created_at_openstack": timestamp(p.get("created_at"), issues),
        "updated_at_openstack": timestamp(p.get("updated_at"), issues),
        "attachments": sorted(attachments, key=lambda a: (a["instance_id"], a["device"] or "")),
        "quality_issues": issues,
    }
