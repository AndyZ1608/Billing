import json
from decimal import Decimal, localcontext
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, inspect, select

from app.core.logging import event
from app.metering.math import rounded, seconds, utc
from app.metering.readiness import billing_readiness
from app.models import (
    BillingAdjustment,
    BillingAudit,
    BillingCycle,
    ChargeRecord,
    Invoice,
    InvoiceChargeLink,
    InvoiceLine,
    InvoiceNumberCounter,
    Product,
    Project,
    RatingRun,
    utcnow,
)
from app.pricing.service import snapshot
from app.rating.completeness import incomplete_usage
from app.rating.money import money


class BillingError(ValueError):
    def __init__(self, code, message, issues=None):
        self.code, self.issues = code, issues or []
        super().__init__(message)


def fail(code, message):
    raise BillingError(code, message)


def audit(db, cloud, actor, action, entity, before=None):
    state = inspect(entity)
    if before is None and state.persistent:
        changes = {attr.key: attr.history.deleted[0] for attr in state.attrs if attr.history.deleted}
        if changes:
            before = {**snapshot(entity), **json.loads(json.dumps(changes, default=str))}
    db.flush()
    db.add(
        BillingAudit(
            cloud_id=cloud,
            actor=actor,
            action=action,
            entity_type=entity.__tablename__,
            entity_id=entity.id,
            before_state=before,
            after_state=snapshot(entity),
        )
    )
    event(
        action,
        actor=actor,
        entity_id=entity.id,
        cloud_id=cloud,
        invoice_id=entity.id if isinstance(entity, Invoice) else getattr(entity, "invoice_id", None),
        billing_cycle_id=entity.id
        if isinstance(entity, BillingCycle)
        else getattr(entity, "billing_cycle_id", None),
        project_id=getattr(entity, "project_id", None),
    )


def slice_charge(charge, start, end):
    a, b = max(utc(start), utc(charge.rated_period_start)), min(utc(end), utc(charge.rated_period_end))
    if a >= b:
        fail("INVALID_PERIOD", "Charge does not intersect the cycle")
    with localcontext() as ctx:
        ctx.prec = 60
        duration = seconds(charge.rated_period_start, charge.rated_period_end)
        lo, hi = (
            seconds(charge.rated_period_start, a) / duration,
            seconds(charge.rated_period_start, b) / duration,
        )
        amount = money(charge.subtotal * hi) - money(charge.subtotal * lo)
        quantity = rounded(charge.rated_quantity * hi) - rounded(charge.rated_quantity * lo)
    return a, b, amount, quantity


class BillingService:
    def __init__(self, settings):
        self.settings, self.cloud = settings, settings.openstack_cloud_id

    def get(self, db, model, identifier):
        value = db.get(model, identifier)
        if value is None:
            fail("NOT_FOUND", "Billing record not found")
        cloud = getattr(value, "cloud_id", None)
        if cloud is None:
            invoice = db.get(Invoice, value.invoice_id)
            cloud = invoice.cloud_id if invoice else None
        if cloud != self.cloud:
            fail("NOT_FOUND", "Billing record not found")
        return value

    def create_cycle(self, db, data, actor):
        if utc(data["period_start"]) >= utc(data["period_end"]):
            fail("INVALID_PERIOD", "Cycle end must follow start")
        data = {**data, "period_start": utc(data["period_start"]), "period_end": utc(data["period_end"])}
        cycle = BillingCycle(
            **data,
            cloud_id=self.cloud,
            billing_timezone=self.settings.billing_timezone,
            usage_calculation_version=self.settings.metering_calculation_version,
            created_by=actor,
        )
        db.add(cycle)
        audit(db, self.cloud, actor, "BILLING_CYCLE_CREATED", cycle)
        return cycle

    def eligible(self, cycle, project=None, currency=None, include_provisional=False):
        query = (
            select(ChargeRecord)
            .join(RatingRun, ChargeRecord.rating_run_id == RatingRun.id)
            .where(
                ChargeRecord.cloud_id == self.cloud,
                ChargeRecord.status == "RATED",
                ChargeRecord.rated_period_start < cycle.period_end,
                ChargeRecord.rated_period_end > cycle.period_start,
                RatingRun.usage_calculation_version == cycle.usage_calculation_version,
            )
        )
        if project is not None:
            query = query.where(ChargeRecord.project_id == project)
        if currency is not None:
            query = query.where(ChargeRecord.currency == currency)
        if not include_provisional and not self.settings.allow_provisional_in_draft:
            query = query.where(ChargeRecord.source_usage_status == "FINAL")
        return query

    def groups(self, db, cycle):
        return list(
            db.execute(
                self.eligible(cycle)
                .with_only_columns(ChargeRecord.project_id, ChargeRecord.currency)
                .distinct()
            )
        )

    def regenerate(self, db, invoice, actor):
        cycle = self.get(db, BillingCycle, invoice.billing_cycle_id)
        if invoice.status != "DRAFT" or cycle.status not in ("OPEN", "CALCULATING", "DRAFT"):
            fail("INVALID_STATE_TRANSITION", "Only draft invoices in an open or draft cycle can regenerate")
        before = snapshot(invoice)
        db.execute(delete(InvoiceChargeLink).where(InvoiceChargeLink.invoice_id == invoice.id))
        db.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
        products = {p.id: p for p in db.scalars(select(Product))}
        lines = {}
        total = Decimal(0)
        with localcontext() as ctx:
            ctx.prec = 60
            for charge in db.scalars(
                self.eligible(cycle, invoice.project_id, invoice.currency).execution_options(yield_per=200)
            ):
                a, b, amount, quantity = slice_charge(charge, cycle.period_start, cycle.period_end)
                if charge.product_id not in products:
                    fail("MISSING_PRODUCT", "Rated charge has no product")
                key = (
                    charge.product_id,
                    charge.product_code,
                    charge.meter_name,
                    charge.unit,
                    charge.unit_price,
                )
                if key not in lines:
                    line = InvoiceLine(
                        id=uuid4(),
                        invoice_id=invoice.id,
                        product_id=charge.product_id,
                        product_code=charge.product_code,
                        product_name=products[charge.product_id].name,
                        meter_name=charge.meter_name,
                        unit=charge.unit,
                        description=products[charge.product_id].name,
                        usage_quantity=Decimal(0),
                        unit_price=charge.unit_price,
                        amount=Decimal(0),
                        currency=charge.currency,
                        period_start=a,
                        period_end=b,
                    )
                    lines[key] = line
                    db.add(line)
                    db.flush()
                line = lines[key]
                line.amount += amount
                line.usage_quantity += quantity
                line.period_start, line.period_end = (
                    min(utc(line.period_start), a),
                    max(utc(line.period_end), b),
                )
                db.add(
                    InvoiceChargeLink(
                        invoice_id=invoice.id,
                        invoice_line_id=line.id,
                        charge_record_id=charge.id,
                        included_start=a,
                        included_end=b,
                        included_amount=amount,
                        included_quantity=quantity,
                    )
                )
                total += amount
            invoice.subtotal = invoice.grand_total = money(total)
        invoice.version += 1
        invoice.updated_at = utcnow()
        audit(db, self.cloud, actor, "DRAFT_INVOICE_REGENERATED", invoice, before)
        return invoice

    def calculate(self, db, cycle, actor):
        if cycle.status not in ("OPEN", "DRAFT"):
            fail("INVALID_STATE_TRANSITION", "Only open or draft cycles can be calculated")
        cycle.status = "CALCULATING"
        audit(db, self.cloud, actor, "BILLING_RUN_STARTED", cycle)
        desired = set(self.groups(db, cycle))
        existing = {
            (i.project_id, i.currency): i
            for i in db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cycle.id))
        }
        for project_id, currency in desired | set(existing):
            invoice = existing.get((project_id, currency))
            if invoice is None:
                project = db.get(Project, (self.cloud, project_id))
                if project is None:
                    fail("MISSING_PROJECT", "A charge references a missing project")
                invoice = Invoice(
                    id=uuid4(),
                    cloud_id=self.cloud,
                    project_id=project_id,
                    project_name_snapshot=project.project_name,
                    billing_cycle_id=cycle.id,
                    billing_timezone=cycle.billing_timezone,
                    currency=currency,
                    period_start=cycle.period_start,
                    period_end=cycle.period_end,
                )
                db.add(invoice)
                audit(db, self.cloud, actor, "DRAFT_INVOICE_CREATED", invoice)
            self.regenerate(db, invoice, actor)
        cycle.status, cycle.updated_at = "DRAFT", utcnow()
        audit(db, self.cloud, actor, "BILLING_RUN_COMPLETED", cycle)
        return cycle

    def review_invoice(self, db, invoice, actor):
        cycle = self.get(db, BillingCycle, invoice.billing_cycle_id)
        if invoice.status == "REVIEW":
            return invoice
        if invoice.status != "DRAFT" or cycle.status not in ("DRAFT", "REVIEW"):
            fail("INVALID_STATE_TRANSITION", "Invoice must be draft in a calculated cycle")
        result = BillingValidationService(self).validate(db, invoice)
        if not result["valid"]:
            raise BillingError(
                "INVOICE_VALIDATION_FAILED", "Resolve invoice validation issues", result["issues"]
            )
        invoice.status = "REVIEW"
        audit(db, self.cloud, actor, "INVOICE_SENT_TO_REVIEW", invoice)
        return invoice

    def review_cycle(self, db, cycle, actor):
        if cycle.status == "REVIEW":
            return cycle
        if cycle.status != "DRAFT":
            fail("INVALID_STATE_TRANSITION", "Cycle must be draft")
        for invoice in db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cycle.id)):
            self.review_invoice(db, invoice, actor)
        cycle.status = "REVIEW"
        audit(db, self.cloud, actor, "BILLING_CYCLE_REVIEW", cycle)
        return cycle

    def finalize_invoice(self, db, invoice, actor):
        if invoice.status == "FINALIZED":
            return invoice
        cycle = self.get(db, BillingCycle, invoice.billing_cycle_id)
        if invoice.status != "REVIEW" or cycle.status != "REVIEW":
            fail("BILLING_CYCLE_NOT_READY", "Invoice and cycle must be in review")
        validation = BillingValidationService(self).validate(db, invoice)
        if not validation["valid"]:
            event(
                "INVOICE_VALIDATION_FAILED", invoice_id=invoice.id, project_id=invoice.project_id, actor=actor
            )
            raise BillingError("INVOICE_VALIDATION_FAILED", "Finalization blocked", validation["issues"])
        counter = db.get(InvoiceNumberCounter, "invoice", with_for_update=True)
        if counter is None:
            counter = InvoiceNumberCounter(name="invoice", value=0)
            db.add(counter)
            db.flush()
        counter.value += 1
        db.flush()
        month = utc(cycle.period_start).astimezone(ZoneInfo(cycle.billing_timezone)).strftime("%Y%m")
        invoice.invoice_number = f"INV-{month}-{counter.value:08d}"
        project = db.get(Project, (self.cloud, invoice.project_id))
        invoice.project_name_snapshot = project.project_name if project else invoice.project_name_snapshot
        now = utcnow()
        # Lock children while parent is REVIEW; parent transition follows in this transaction.
        for link in db.scalars(select(InvoiceChargeLink).where(InvoiceChargeLink.invoice_id == invoice.id)):
            link.locked_at = now
        db.flush()
        invoice.status, invoice.finalized_at, invoice.issued_at = "FINALIZED", now, now
        audit(db, self.cloud, actor, "INVOICE_FINALIZED", invoice)
        event("CHARGE_LOCKED", invoice_id=invoice.id, project_id=invoice.project_id, actor=actor)
        return invoice

    def finalize_cycle(self, db, cycle, actor):
        if cycle.status == "FINALIZED":
            return cycle
        if cycle.status != "REVIEW":
            fail("INVALID_STATE_TRANSITION", "Cycle must be in review")
        invoices = list(db.scalars(select(Invoice).where(Invoice.billing_cycle_id == cycle.id)))
        if not invoices or any(i.status != "FINALIZED" for i in invoices):
            fail("BILLING_CYCLE_NOT_READY", "All invoices must be finalized")
        if set(self.groups(db, cycle)) != {(i.project_id, i.currency) for i in invoices}:
            fail("BILLING_CYCLE_NOT_READY", "New charge groups require billing review")
        if incomplete_usage(
            db, self.cloud, cycle.usage_calculation_version, cycle.period_start, cycle.period_end
        ):
            fail("INVOICE_HAS_UNRATED_USAGE", "Cycle has incomplete rated usage")
        readiness = billing_readiness(
            db, self.cloud, cycle.usage_calculation_version, cycle.period_start, cycle.period_end
        )
        if readiness["provisional_periods"] or readiness["quality_periods"]:
            fail("METERING_DATA_INCOMPLETE", "Cycle contains open or unresolved metering periods")
        for invoice in invoices:
            result = BillingValidationService(self).validate(db, invoice)
            if not result["valid"]:
                raise BillingError("BILLING_CYCLE_NOT_READY", "Invoice validation failed", result["issues"])
        cycle.status, cycle.finalized_at = "FINALIZED", utcnow()
        audit(db, self.cloud, actor, "BILLING_CYCLE_FINALIZED", cycle)
        return cycle

    def close(self, db, cycle, actor):
        if cycle.status == "CLOSED":
            return cycle
        if cycle.status != "FINALIZED":
            fail("INVALID_STATE_TRANSITION", "Only finalized cycles can close")
        cycle.status, cycle.closed_at = "CLOSED", utcnow()
        audit(db, self.cloud, actor, "BILLING_CYCLE_CLOSED", cycle)
        return cycle

    def adjustment(self, db, invoice, data, actor):
        if invoice.status != "FINALIZED":
            fail("INVALID_STATE_TRANSITION", "Adjustments require a finalized invoice")
        amount = data.pop("amount")
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            fail("INVALID_AMOUNT", "Supply a positive Decimal adjustment magnitude")
        if data["currency"] != invoice.currency:
            fail("CURRENCY_CONFLICT", "Adjustment currency must match invoice")
        value = BillingAdjustment(
            **data,
            invoice_id=invoice.id,
            project_id=invoice.project_id,
            amount=-amount if data["type"] == "CREDIT" else amount,
            created_by=actor,
        )
        db.add(value)
        audit(db, self.cloud, actor, "ADJUSTMENT_CREATED", value)
        return value

    def adjustment_transition(self, db, value, action, actor):
        target, source = ("APPROVED", "DRAFT") if action == "approve" else ("APPLIED", "APPROVED")
        if value.status == target:
            return value
        if value.status != source:
            fail("INVALID_STATE_TRANSITION", f"Adjustment must be {source}")
        value.status = target
        if action == "approve":
            value.approved_at, value.approved_by = utcnow(), actor
        else:
            value.applied_at = utcnow()
        audit(db, self.cloud, actor, "ADJUSTMENT_" + target, value)
        return value

    def totals(self, db, invoice):
        adjustment = db.scalar(
            select(func.coalesce(func.sum(BillingAdjustment.amount), 0)).where(
                BillingAdjustment.invoice_id == invoice.id, BillingAdjustment.status == "APPLIED"
            )
        )
        with localcontext() as ctx:
            ctx.prec = 60
            return dict(
                original_total=invoice.grand_total,
                applied_adjustment_total=adjustment,
                net_amount=invoice.grand_total + adjustment,
            )


class BillingValidationService:
    def __init__(self, service):
        self.service = service

    def validate(self, db, invoice):
        s = self.service
        cycle = s.get(db, BillingCycle, invoice.billing_cycle_id)
        issues = []

        def issue(code, message, severity="ERROR"):
            issues.append(dict(code=code, severity=severity, message=message))

        if utc(cycle.period_end) > utcnow():
            issue("BILLING_CYCLE_NOT_READY", "Billing period has not ended")
        if not s.settings.allow_provisional_in_final and db.scalar(
            s.eligible(cycle, invoice.project_id, invoice.currency, include_provisional=True)
            .with_only_columns(func.count())
            .where(ChargeRecord.source_usage_status != "FINAL")
        ):
            issue("INVOICE_HAS_PROVISIONAL_CHARGES", "Project has provisional charges in the cycle")
        readiness = billing_readiness(
            db,
            s.cloud,
            cycle.usage_calculation_version,
            cycle.period_start,
            cycle.period_end,
            invoice.project_id,
        )
        if readiness["quality_periods"]:
            issue("METERING_DATA_INCOMPLETE", "Unknown allocation or state prevents complete billing")
        if readiness["provisional_periods"]:
            issue(
                "INVOICE_HAS_PROVISIONAL_USAGE",
                "Open or unmetered source periods must be finalized by metering first",
            )
        missing = incomplete_usage(
            db,
            s.cloud,
            cycle.usage_calculation_version,
            cycle.period_start,
            cycle.period_end,
            invoice.project_id,
        )
        if missing:
            issue("INVOICE_HAS_UNRATED_USAGE", f"{missing} usage records have missing or unrated coverage")
        lines = {
            line.id: line
            for line in db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
        }
        if not lines:
            issue("EMPTY_INVOICE", "Invoice has no lines")
        expected = {c.id: c for c in db.scalars(s.eligible(cycle, invoice.project_id, invoice.currency))}
        links = list(db.scalars(select(InvoiceChargeLink).where(InvoiceChargeLink.invoice_id == invoice.id)))
        if set(expected) != {link.charge_record_id for link in links}:
            issue("STALE_CHARGE_SELECTION", "Source charges changed; regenerate the draft")
        amounts, quantities = {}, {}
        with localcontext() as ctx:
            ctx.prec = 60
            for link in links:
                charge, line = db.get(ChargeRecord, link.charge_record_id), lines.get(link.invoice_line_id)
                if charge is None or line is None:
                    issue("MISSING_SOURCE_TRACE", "Source charge or invoice line missing")
                    continue
                if charge.status != "RATED":
                    issue("STALE_CHARGE_SELECTION", "Source charge is no longer rated")
                    continue
                a, b, amount, quantity = slice_charge(charge, cycle.period_start, cycle.period_end)
                if (
                    utc(link.included_start),
                    utc(link.included_end),
                    link.included_amount,
                    link.included_quantity,
                ) != (a, b, amount, quantity):
                    issue(
                        "RECONCILIATION_FAILED",
                        "Linked amount, quantity or period differs from stored charge allocation",
                    )
                if (line.product_id, line.product_code, line.meter_name, line.unit, line.unit_price) != (
                    charge.product_id,
                    charge.product_code,
                    charge.meter_name,
                    charge.unit,
                    charge.unit_price,
                ):
                    issue("RECONCILIATION_FAILED", "Line grouping differs from source charge")
                if charge.currency != invoice.currency or line.currency != invoice.currency:
                    issue("CURRENCY_CONFLICT", "Every line and charge must use invoice currency")
                if charge.source_usage_status != "FINAL" and not s.settings.allow_provisional_in_final:
                    issue("INVOICE_HAS_PROVISIONAL_CHARGES", "Provisional charges cannot be finalized")
                conflict = db.scalar(
                    select(InvoiceChargeLink.id)
                    .where(
                        InvoiceChargeLink.charge_record_id == charge.id,
                        InvoiceChargeLink.invoice_id != invoice.id,
                        InvoiceChargeLink.locked_at.is_not(None),
                        InvoiceChargeLink.included_start < b,
                        InvoiceChargeLink.included_end > a,
                    )
                    .limit(1)
                )
                if conflict:
                    issue(
                        "CHARGE_ALREADY_BILLED",
                        "Overlapping charge portion belongs to another finalized invoice",
                    )
                if invoice.status == "FINALIZED" and link.locked_at is None:
                    issue("MISSING_CHARGE_LOCK", "Finalized source link is not locked")
                amounts[line.id] = amounts.get(line.id, Decimal(0)) + link.included_amount
                quantities[line.id] = quantities.get(line.id, Decimal(0)) + link.included_quantity
            for line in lines.values():
                if (
                    line.id not in amounts
                    or amounts[line.id] != line.amount
                    or quantities[line.id] != line.usage_quantity
                ):
                    issue("RECONCILIATION_FAILED", "Invoice line does not reconcile with source links")
                if line.amount == 0:
                    issue("ZERO_VALUE_LINE", "Zero-value rated line retained for traceability", "WARNING")
            total = sum((line.amount for line in lines.values()), Decimal(0))
            if (
                total != invoice.subtotal
                or invoice.grand_total != invoice.subtotal + invoice.adjustment_total + invoice.tax_total
            ):
                issue("RECONCILIATION_FAILED", "Invoice totals do not reconcile")
        return dict(
            valid=not any(i["severity"] == "ERROR" for i in issues),
            issues=issues,
            unrated_usage_count=missing,
            metering_readiness=readiness,
        )
