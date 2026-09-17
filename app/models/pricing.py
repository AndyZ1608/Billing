from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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


class Identity:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Product(Identity, Base):
    __tablename__ = "billing_products"
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(2000), default="")
    meter_name: Mapped[str] = mapped_column(String(64), unique=True)
    unit: Mapped[str] = mapped_column(String(30))
    service_category: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceBook(Identity, Base):
    __tablename__ = "price_books"
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(2000), default="")
    currency: Mapped[str] = mapped_column(String(3))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("currency IN ('VND','USD')", name="ck_book_currency"),)


class Effective:
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PriceBookVersion(Identity, Effective, Base):
    __tablename__ = "price_book_versions"
    price_book_id: Mapped[UUID] = mapped_column(ForeignKey("price_books.id"))
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(100), default="local-admin")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("price_book_id", "version", name="uq_book_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="ck_price_version_bounds"
        ),
        CheckConstraint("status IN ('DRAFT','ACTIVE','RETIRED')", name="ck_price_version_status"),
        Index("ix_price_version_effective", "price_book_id", "status", "effective_from", "effective_to"),
    )


class PriceRule(Identity, Base):
    __tablename__ = "price_rules"
    price_book_version_id: Mapped[UUID] = mapped_column(ForeignKey("price_book_versions.id"))
    product_id: Mapped[UUID] = mapped_column(ForeignKey("billing_products.id"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    billing_unit: Mapped[str] = mapped_column(String(30))
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=Decimal(0))
    rounding_mode: Mapped[str] = mapped_column(String(30), default="HALF_EVEN")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("price_book_version_id", "product_id", name="uq_rule_product"),
        CheckConstraint("unit_price >= 0 AND minimum_quantity = 0", name="ck_rule_simple_price"),
        CheckConstraint("rounding_mode = 'HALF_EVEN'", name="ck_rule_rounding"),
    )


class ProjectAssignment(Identity, Effective, Base):
    __tablename__ = "project_price_book_assignments"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID | None] = mapped_column(Uuid)
    price_book_id: Mapped[UUID] = mapped_column(ForeignKey("price_books.id"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["cloud_id", "project_id"], ["projects.cloud_id", "projects.project_id"]),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_assignment_bounds"),
        Index("ix_assignment_effective", "cloud_id", "project_id", "effective_from", "effective_to"),
    )


class ProjectOverride(Identity, Effective, Base):
    __tablename__ = "project_price_overrides"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID] = mapped_column(Uuid)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("billing_products.id"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(String(2000))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["cloud_id", "project_id"], ["projects.cloud_id", "projects.project_id"]),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_override_bounds"),
        CheckConstraint("unit_price >= 0", name="ck_override_price"),
        CheckConstraint("currency IN ('VND','USD')", name="ck_override_currency"),
        Index(
            "ix_override_effective", "cloud_id", "project_id", "product_id", "effective_from", "effective_to"
        ),
    )


class PricingAudit(Identity, Base):
    __tablename__ = "pricing_audit_log"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[UUID] = mapped_column(Uuid)
    before: Mapped[dict | None] = mapped_column(json_type)
    after: Mapped[dict | None] = mapped_column(json_type)
    actor: Mapped[str] = mapped_column(String(100))
    __table_args__ = (Index("ix_pricing_audit_time", "cloud_id", "created_at"),)


class RatingRun(Identity, Base):
    __tablename__ = "rating_runs"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    usage_records_processed: Mapped[int] = mapped_column(Integer, default=0)
    charge_records_created: Mapped[int] = mapped_column(Integer, default=0)
    charge_records_skipped: Mapped[int] = mapped_column(Integer, default=0)
    unrated_records: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(json_type, default=list)
    rating_version: Mapped[str] = mapped_column(String(40))
    usage_calculation_version: Mapped[str] = mapped_column(String(40))
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    actor: Mapped[str] = mapped_column(String(100))
    __table_args__ = (Index("ix_rating_runs_cloud_time", "cloud_id", "started_at"),)


class ChargeRecord(Identity, Base):
    __tablename__ = "charge_records"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID] = mapped_column(Uuid)
    resource_type: Mapped[str] = mapped_column(String(20))
    resource_id: Mapped[UUID] = mapped_column(Uuid)
    usage_record_id: Mapped[UUID] = mapped_column(ForeignKey("usage_records.usage_record_id"))
    product_id: Mapped[UUID | None] = mapped_column(ForeignKey("billing_products.id"))
    product_code: Mapped[str | None] = mapped_column(String(64))
    service_category: Mapped[str | None] = mapped_column(String(40))
    meter_name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(30))
    rated_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rated_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    allocated_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    usage_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    rated_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(38, 8))
    pricing_source: Mapped[str | None] = mapped_column(String(30))
    price_book_id: Mapped[UUID | None] = mapped_column(ForeignKey("price_books.id"))
    price_book_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("price_book_versions.id"))
    price_book_version: Mapped[str | None] = mapped_column(String(64))
    price_rule_id: Mapped[UUID | None] = mapped_column(ForeignKey("price_rules.id"))
    project_price_override_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_price_overrides.id"))
    assignment_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_price_book_assignments.id"))
    pricing_effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pricing_effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    formula: Mapped[str] = mapped_column(
        String(100), default="allocated_quantity * seconds / 3600 * unit_price"
    )
    rating_version: Mapped[str] = mapped_column(String(40))
    source_usage_status: Mapped[str] = mapped_column(String(20), default="FINAL")
    status: Mapped[str] = mapped_column(String(20))
    unrated_reason: Mapped[str | None] = mapped_column(String(64))
    rating_run_id: Mapped[UUID] = mapped_column(ForeignKey("rating_runs.id"))
    superseded_by_rating_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("rating_runs.id"))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "rated_period_end > rated_period_start AND duration_seconds > 0", name="ck_charge_bounds"
        ),
        CheckConstraint("rated_quantity >= 0 AND allocated_quantity >= 0", name="ck_charge_quantity"),
        CheckConstraint(
            "status IN ('RATED','UNRATED','SUPERSEDED') AND source_usage_status = 'FINAL'",
            name="ck_charge_status",
        ),
        CheckConstraint(
            "status != 'RATED' OR (subtotal IS NOT NULL AND subtotal >= 0 "
            "AND unit_price IS NOT NULL AND unit_price >= 0 AND currency IN ('VND','USD'))",
            name="ck_charge_rated",
        ),
        Index(
            "uq_charge_active_segment",
            "usage_record_id",
            "rated_period_start",
            "rated_period_end",
            unique=True,
            postgresql_where=text("status != 'SUPERSEDED'"),
            sqlite_where=text("status != 'SUPERSEDED'"),
        ),
        Index("ix_charge_project_time", "cloud_id", "project_id", "rated_period_start", "rated_period_end"),
        Index("ix_charge_resource_meter", "cloud_id", "resource_id", "meter_name", "rated_period_start"),
        Index("ix_charge_status_currency", "cloud_id", "status", "currency"),
        Index("ix_charge_product", "product_id"),
        Index("ix_charge_run", "rating_run_id"),
    )
