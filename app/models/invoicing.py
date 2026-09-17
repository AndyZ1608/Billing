from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, json_type, utcnow
from app.models.pricing import Identity


class BillingCycle(Identity, Base):
    __tablename__ = "billing_cycles"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    billing_timezone: Mapped[str] = mapped_column(String(64))
    usage_calculation_version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_by: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("cloud_id", "code"),
        CheckConstraint("period_start < period_end"),
        CheckConstraint("status IN ('OPEN','CALCULATING','DRAFT','REVIEW','FINALIZED','CLOSED')"),
        Index("ix_billing_cycle_period", "cloud_id", "period_start", "period_end"),
    )


class Invoice(Identity, Base):
    __tablename__ = "invoices"
    invoice_number: Mapped[str | None] = mapped_column(String(64), unique=True)
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    project_id: Mapped[UUID] = mapped_column(Uuid)
    project_name_snapshot: Mapped[str] = mapped_column(String(255))
    billing_cycle_id: Mapped[UUID] = mapped_column(ForeignKey("billing_cycles.id"))
    billing_timezone: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(38, 8), default=Decimal(0))
    adjustment_total: Mapped[Decimal] = mapped_column(Numeric(38, 8), default=Decimal(0))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(38, 8), default=Decimal(0))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(38, 8), default=Decimal(0))
    version: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str] = mapped_column(String(2000), default="")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("cloud_id", "project_id", "billing_cycle_id", "currency"),
        CheckConstraint("status IN ('DRAFT','REVIEW','FINALIZED','VOID')"),
        CheckConstraint("currency IN ('VND','USD')"),
        CheckConstraint(
            "subtotal >= 0 AND tax_total = 0 AND adjustment_total = 0 AND grand_total = subtotal"
        ),
        CheckConstraint("period_start < period_end"),
        Index("ix_invoice_cycle_status", "billing_cycle_id", "status"),
        Index("ix_invoice_project", "cloud_id", "project_id"),
    )


class InvoiceLine(Identity, Base):
    __tablename__ = "invoice_lines"
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    line_type: Mapped[str] = mapped_column(String(20), default="USAGE")
    product_id: Mapped[UUID] = mapped_column(ForeignKey("billing_products.id"))
    product_code: Mapped[str] = mapped_column(String(64))
    product_name: Mapped[str] = mapped_column(String(255))
    meter_name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(String(1000))
    usage_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    currency: Mapped[str] = mapped_column(String(3))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("line_type = 'USAGE' AND amount >= 0 AND usage_quantity >= 0 AND unit_price >= 0"),
        CheckConstraint("period_start < period_end"),
    )


class InvoiceChargeLink(Identity, Base):
    __tablename__ = "invoice_charge_links"
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    invoice_line_id: Mapped[UUID] = mapped_column(ForeignKey("invoice_lines.id"), index=True)
    charge_record_id: Mapped[UUID] = mapped_column(ForeignKey("charge_records.id"), index=True)
    included_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    included_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    included_amount: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    included_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("invoice_id", "charge_record_id"),
        CheckConstraint("included_start < included_end"),
        CheckConstraint("included_amount >= 0 AND included_quantity >= 0"),
    )


class BillingAdjustment(Identity, Base):
    __tablename__ = "billing_adjustments"
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    type: Mapped[str] = mapped_column(String(10))
    reason_code: Mapped[str] = mapped_column(String(64))
    reason_text: Mapped[str] = mapped_column(String(2000))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(100))
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','APPROVED','APPLIED')"),
        CheckConstraint("(type='CREDIT' AND amount < 0) OR (type='DEBIT' AND amount > 0)"),
        CheckConstraint("length(reason_code)>0 AND length(reason_text)>0"),
    )


class BillingAudit(Identity, Base):
    __tablename__ = "billing_audit_log"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"))
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    before_state: Mapped[dict | None] = mapped_column(json_type)
    after_state: Mapped[dict] = mapped_column(json_type)


class InvoiceNumberCounter(Base):
    __tablename__ = "invoice_number_counters"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer)
