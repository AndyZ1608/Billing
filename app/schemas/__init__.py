from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_utc(self, value):
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
        return value


class Seen(ReadModel):
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
    missing_since: datetime | None
    missing_scans: int
    is_missing: bool


class ProjectOut(Seen):
    cloud_id: UUID
    project_id: UUID
    project_name: str
    domain_id: str | None
    enabled: bool | None
    is_placeholder: bool


class InstanceOut(Seen):
    instance_id: UUID
    project_id: UUID
    instance_name: str
    status: str
    created_at_openstack: datetime | None
    updated_at_openstack: datetime | None
    deleted_at_openstack: datetime | None
    flavor_id: str | None
    flavor_name: str | None
    vcpus: int | None
    ram_mb: int | None
    ram_gb: float | None
    root_disk_gb: int | None
    ephemeral_disk_gb: int | None
    boot_source: str
    host: str | None
    availability_zone: str | None
    quality_issues: list[str]


class Attachment(ReadModel):
    instance_id: UUID
    device: str | None


class VolumeOut(Seen):
    volume_id: UUID
    project_id: UUID
    volume_name: str
    status: str
    volume_type: str | None
    size_gb: int | None
    bootable: bool | None
    created_at_openstack: datetime | None
    updated_at_openstack: datetime | None
    attachments: list[Attachment]
    quality_issues: list[str]


class CloudOut(ReadModel):
    cloud_id: UUID
    name: str
    region: str
    connection_status: str
    last_attempt_at: datetime | None
    last_successful_sync: datetime | None
    last_failed_sync: datetime | None
    service_status: dict


class RunOut(ReadModel):
    sync_run_id: UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    projects_found: int
    instances_found: int
    volumes_found: int
    errors: list[dict]
    services: dict
    duration_seconds: Decimal | None


class Quantities(ReadModel):
    instance_count: int
    vcpu_count: int
    ram_gb: float
    nova_root_disk_gb: int
    nova_ephemeral_disk_gb: int
    cinder_volume_count: int
    cinder_volume_gb: int
    incomplete_instances: int
    incomplete_volumes: int
    pending_missing_instances: int
    pending_missing_volumes: int


class ProjectSummary(Quantities):
    project_id: UUID
    project_name: str
    enabled: bool | None
    is_placeholder: bool
    is_missing: bool


class CurrentSummary(Quantities):
    projects: int
    interpretation: str = "Current allocated inventory; not resource-hours or monetary charges."
    units: dict = {"ram_gb": "GiB (1024 MiB)", "disk_gb": "GiB (OpenStack disk/size units)"}


class Page[T](ReadModel):
    items: list[T]
    total: int
    offset: int
    limit: int
