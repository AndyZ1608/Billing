from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, json_type, utcnow


class StatePeriod(Base):
    __tablename__ = "resource_state_periods"
    period_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID] = mapped_column(Uuid)
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    resource_name: Mapped[str] = mapped_column(String(255))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(64))
    flavor_id: Mapped[str | None] = mapped_column(String(255))
    flavor_name: Mapped[str | None] = mapped_column(String(255))
    vcpus: Mapped[int | None] = mapped_column(Integer)
    ram_mb: Mapped[int | None] = mapped_column(Integer)
    root_disk_gb: Mapped[int | None] = mapped_column(Integer)
    ephemeral_disk_gb: Mapped[int | None] = mapped_column(Integer)
    boot_source: Mapped[str | None] = mapped_column(String(20))
    volume_size_gb: Mapped[int | None] = mapped_column(Integer)
    volume_type: Mapped[str | None] = mapped_column(String(255))
    openstack_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    openstack_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_confidence: Mapped[str] = mapped_column(String(20))
    effective_time_source: Mapped[str] = mapped_column(String(40), default="OBSERVATION")
    payload_hash: Mapped[str] = mapped_column(String(64))
    quality_issues: Mapped[list] = mapped_column(json_type, default=list)
    source_observation_id: Mapped[UUID] = mapped_column(ForeignKey("resource_observations.observation_id"))
    closing_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource_observations.observation_id")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_reason: Mapped[str | None] = mapped_column(String(40))
    first_missing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_period_positive"),
        CheckConstraint("resource_type IN ('INSTANCE', 'VOLUME')", name="ck_period_type"),
        Index("ix_period_resource_time", "cloud_id", "resource_type", "resource_id", "valid_from"),
        Index("ix_period_project_time", "cloud_id", "project_id", "valid_from", "valid_to"),
        Index("ix_period_closed", "cloud_id", "closed_at"),
        Index(
            "uq_period_open",
            "cloud_id",
            "resource_type",
            "resource_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )


class LifecycleHead(Base):
    __tablename__ = "resource_lifecycle_heads"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    latest_period_id: Mapped[UUID] = mapped_column(ForeignKey("resource_state_periods.period_id"))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_payload_hash: Mapped[str] = mapped_column(String(64))


class MeteringPolicyVersion(Base):
    __tablename__ = "metering_policy_versions"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    calculation_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    policy_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(json_type)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MeteringRun(Base):
    __tablename__ = "metering_runs"
    metering_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    calculation_version: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    requested_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_periods_processed: Mapped[int] = mapped_column(Integer, default=0)
    usage_records_created: Mapped[int] = mapped_column(Integer, default=0)
    usage_records_reused: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(json_type, default=list)
    __table_args__ = (Index("ix_metering_runs_cloud_time", "cloud_id", "started_at"),)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    usage_record_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID] = mapped_column(Uuid)
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    meter_name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(30))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    allocated_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    usage_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    source_state_period_id: Mapped[UUID] = mapped_column(ForeignKey("resource_state_periods.period_id"))
    calculation_version: Mapped[str] = mapped_column(String(40))
    metering_run_id: Mapped[UUID] = mapped_column(ForeignKey("metering_runs.metering_run_id"))
    status: Mapped[str] = mapped_column(String(20), default="FINAL")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["cloud_id", "calculation_version"],
            ["metering_policy_versions.cloud_id", "metering_policy_versions.calculation_version"],
        ),
        UniqueConstraint(
            "source_state_period_id",
            "meter_name",
            "period_start",
            "period_end",
            "calculation_version",
            name="uq_usage_identity",
        ),
        CheckConstraint(
            "period_end > period_start AND duration_seconds > 0", name="ck_usage_positive_duration"
        ),
        CheckConstraint("allocated_quantity >= 0 AND usage_quantity >= 0", name="ck_usage_nonnegative"),
        CheckConstraint("status = 'FINAL'", name="ck_usage_final"),
        Index("ix_usage_rating_scan", "cloud_id", "calculation_version", "usage_record_id"),
        Index("ix_usage_project_time", "cloud_id", "project_id", "period_start", "period_end"),
        Index("ix_usage_resource_meter", "cloud_id", "resource_id", "meter_name", "period_start"),
    )
