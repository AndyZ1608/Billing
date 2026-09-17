from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


json_type = JSON().with_variant(JSONB(), "postgresql")


class Cloud(Base):
    __tablename__ = "clouds"
    cloud_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(255))
    connection_status: Mapped[str] = mapped_column(String(20), default="NOT_CHECKED")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_status: Mapped[dict] = mapped_column(json_type, default=dict)


class SeenMixin:
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_scans: Mapped[int] = mapped_column(Integer, default=0)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)


class Project(SeenMixin, Base):
    __tablename__ = "projects"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_name: Mapped[str] = mapped_column(String(255))
    domain_id: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)


class Instance(SeenMixin, Base):
    __tablename__ = "instances"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    instance_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64))
    created_at_openstack: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_openstack: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at_openstack: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flavor_id: Mapped[str | None] = mapped_column(String(255))
    flavor_name: Mapped[str | None] = mapped_column(String(255))
    vcpus: Mapped[int | None] = mapped_column(Integer)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    root_disk_gb: Mapped[int | None] = mapped_column(Integer)
    ephemeral_disk_gb: Mapped[int | None] = mapped_column(Integer)
    boot_source: Mapped[str] = mapped_column(String(20))
    host: Mapped[str | None] = mapped_column(String(255))
    availability_zone: Mapped[str | None] = mapped_column(String(255))
    quality_issues: Mapped[list] = mapped_column(json_type, default=list)
    raw_payload: Mapped[dict | None] = mapped_column(json_type)
    __table_args__ = (
        ForeignKeyConstraint(["cloud_id", "project_id"], ["projects.cloud_id", "projects.project_id"]),
        Index("ix_instances_cloud_project", "cloud_id", "project_id"),
    )

    @property
    def ram_gb(self):
        return Decimal(self.ram_mb) / Decimal(1024) if self.ram_mb is not None else None


class Volume(SeenMixin, Base):
    __tablename__ = "volumes"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    volume_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    volume_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64))
    volume_type: Mapped[str | None] = mapped_column(String(255))
    size_gb: Mapped[int | None] = mapped_column(Integer)
    deleted_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bootable: Mapped[bool | None] = mapped_column(Boolean)
    created_at_openstack: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_openstack: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachments: Mapped[list] = mapped_column(json_type, default=list)
    quality_issues: Mapped[list] = mapped_column(json_type, default=list)
    raw_payload: Mapped[dict | None] = mapped_column(json_type)
    __table_args__ = (
        ForeignKeyConstraint(["cloud_id", "project_id"], ["projects.cloud_id", "projects.project_id"]),
        Index("ix_volumes_cloud_project", "cloud_id", "project_id"),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    sync_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    projects_found: Mapped[int] = mapped_column(Integer, default=0)
    instances_found: Mapped[int] = mapped_column(Integer, default=0)
    volumes_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(json_type, default=list)
    services: Mapped[dict] = mapped_column(json_type, default=dict)

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        elapsed = self.finished_at - self.started_at
        return Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(elapsed.microseconds) / Decimal(
            1000000
        )


class Observation(Base):
    __tablename__ = "resource_observations"
    observation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    sync_run_id: Mapped[UUID] = mapped_column(ForeignKey("sync_runs.sync_run_id"))
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event: Mapped[str] = mapped_column(String(20), default="SEEN")
    normalized_payload: Mapped[dict] = mapped_column(json_type)
    project_id: Mapped[UUID | None] = mapped_column(Uuid)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        Index("ix_observations_resource_time", "cloud_id", "resource_type", "resource_id", "observed_at"),
        Index("ix_observations_run", "sync_run_id"),
    )


# Register the additive Phase 2 models with the same Alembic metadata.
from app.models.history import (  # noqa: E402,F401
    LifecycleHead,
    MeteringPolicyVersion,
    MeteringRun,
    StatePeriod,
    UsageRecord,
)
from app.models.invoicing import (  # noqa: E402,F401
    BillingAdjustment,
    BillingAudit,
    BillingCycle,
    Invoice,
    InvoiceChargeLink,
    InvoiceLine,
    InvoiceNumberCounter,
)
from app.models.pricing import (  # noqa: E402,F401
    ChargeRecord,
    PriceBook,
    PriceBookVersion,
    PriceRule,
    PricingAudit,
    Product,
    ProjectAssignment,
    ProjectOverride,
    RatingRun,
)
