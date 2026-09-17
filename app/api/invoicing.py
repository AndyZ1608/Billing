import secrets
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.pricing import Input, response, row
from app.api.routes import DB, Limit, Offset
from app.core.jobs import JobBusy
from app.invoicing.service import BillingError, BillingService, BillingValidationService, audit
from app.metering.readiness import billing_readiness
from app.models import (
    BillingAdjustment,
    BillingAudit,
    BillingCycle,
    ChargeRecord,
    Invoice,
    InvoiceChargeLink,
    InvoiceLine,
    RatingRun,
)
from app.rating.completeness import incomplete_usage

router = APIRouter(tags=["Billing and invoices"])


def identity(request, admin):
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "Use application/json")
    settings = request.app.state.settings
    tokens = [settings.billing_admin_token.get_secret_value()]
    if not admin:
        tokens.append(settings.billing_operator_token.get_secret_value())
    provided = request.headers.get("Authorization", "")
    if not any(token and secrets.compare_digest(provided, "Bearer " + token) for token in tokens):
        raise HTTPException(
            403,
            "A configured billing admin token is required"
            if admin
            else "A configured billing operator or admin token is required",
        )
    # The token capability is the authenticated actor; client labels cannot impersonate an identity.
    return (
        "billing-admin"
        if tokens[0] and secrets.compare_digest(provided, "Bearer " + tokens[0])
        else "billing-operator"
    )


def admin(request: Request):
    return identity(request, True)


def operator(request: Request):
    return identity(request, False)


Admin = Annotated[str, Depends(admin)]
Operator = Annotated[str, Depends(operator)]


class CycleInput(Input):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(default_factory=lambda: "CYCLE-" + uuid4().hex[:16], max_length=64, min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime

    @model_validator(mode="after")
    def bounds(self):
        if self.period_start >= self.period_end:
            raise ValueError("Cycle end must follow start")
        return self


class AdjustmentInput(Input):
    type: Literal["CREDIT", "DEBIT"]
    amount: Decimal = Field(gt=0, max_digits=38, decimal_places=8)
    currency: Literal["VND", "USD"]
    reason_code: Literal[
        "BILLING_CORRECTION",
        "SERVICE_CREDIT",
        "MANUAL_CORRECTION",
        "RESOURCE_DISPUTE",
        "PRICING_CORRECTION",
        "OTHER",
    ]
    reason_text: str = Field(min_length=1, max_length=2000)

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_only(cls, value):
        if isinstance(value, float):
            raise ValueError("Send money as a decimal string")
        return value

    @field_validator("reason_text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


def service(request):
    return BillingService(request.app.state.settings)


def mutate(request, operation):
    try:
        with request.app.state.rating.lock.held(), request.app.state.sessions.begin() as db:
            value = operation(db, service(request))
            db.flush()
            result = row(value)
        return response(result)
    except BillingError as exc:
        raise HTTPException(
            404 if exc.code == "NOT_FOUND" else 409, dict(code=exc.code, message=str(exc), issues=exc.issues)
        ) from None
    except JobBusy:
        raise HTTPException(
            409, dict(code="BILLING_BUSY", message="Another cloud financial operation is running")
        ) from None
    except (IntegrityError, OperationalError):
        raise HTTPException(
            409,
            dict(
                code="BILLING_CONFLICT",
                message="Conflicting financial operation or constraint; reload and retry",
            ),
        ) from None


def get(request, db, model, identifier):
    try:
        return service(request).get(db, model, identifier)
    except BillingError as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/api/v1/billing/cycles")
def create_cycle(data: CycleInput, request: Request, actor: Operator):
    return mutate(request, lambda db, s: s.create_cycle(db, data.model_dump(), actor))


@router.get("/api/v1/billing/cycles")
def cycles(request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    return response(
        [
            row(v)
            for v in db.scalars(
                select(BillingCycle)
                .where(BillingCycle.cloud_id == service(request).cloud)
                .order_by(BillingCycle.period_start.desc(), BillingCycle.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    )


@router.get("/api/v1/billing/cycles/{identifier}")
def cycle_detail(identifier: UUID, request: Request, db: DB):
    return response(row(get(request, db, BillingCycle, identifier)))


@router.post("/api/v1/billing/cycles/{identifier}/calculate")
def calculate(identifier: UUID, request: Request, actor: Operator):
    return mutate(request, lambda db, s: s.calculate(db, s.get(db, BillingCycle, identifier), actor))


@router.post("/api/v1/billing/cycles/{identifier}/review")
def review_cycle(identifier: UUID, request: Request, actor: Operator):
    return mutate(request, lambda db, s: s.review_cycle(db, s.get(db, BillingCycle, identifier), actor))


@router.post("/api/v1/billing/cycles/{identifier}/reopen")
def reopen_cycle(identifier: UUID, request: Request, actor: Admin):
    def action(db, s):
        cycle = s.get(db, BillingCycle, identifier)
        if cycle.status != "REVIEW":
            raise BillingError("INVALID_STATE_TRANSITION", "Only a review cycle can be reopened")
        invoices = list(db.scalars(select(Invoice).where(Invoice.billing_cycle_id == identifier)))
        if any(i.status == "FINALIZED" for i in invoices):
            raise BillingError(
                "INVALID_STATE_TRANSITION", "A cycle containing finalized invoices cannot reopen"
            )
        for invoice in invoices:
            invoice.status = "DRAFT"
            audit(db, s.cloud, actor, "INVOICE_REOPENED", invoice)
        cycle.status = "DRAFT"
        audit(db, s.cloud, actor, "BILLING_CYCLE_REOPENED", cycle)
        return cycle

    return mutate(request, action)


@router.post("/api/v1/billing/cycles/{identifier}/{action}")
def cycle_action(identifier: UUID, action: Literal["finalize", "close"], request: Request, actor: Admin):
    return mutate(
        request,
        lambda db, s: getattr(s, "finalize_cycle" if action == "finalize" else "close")(
            db, s.get(db, BillingCycle, identifier), actor
        ),
    )


@router.get("/api/v1/invoices")
def invoices(
    request: Request,
    db: DB,
    limit: Limit = 100,
    offset: Offset = 0,
    project_id: UUID | None = None,
    billing_cycle_id: UUID | None = None,
    status: Literal["DRAFT", "REVIEW", "FINALIZED", "VOID"] | None = None,
    currency: Literal["VND", "USD"] | None = None,
    period_start: AwareDatetime | None = None,
    period_end: AwareDatetime | None = None,
    search: str = Query("", max_length=255),
    sort: Literal["newest", "oldest", "amount"] = "newest",
):
    query = select(Invoice).where(Invoice.cloud_id == service(request).cloud)
    for column, value in (
        (Invoice.project_id, project_id),
        (Invoice.billing_cycle_id, billing_cycle_id),
        (Invoice.status, status),
        (Invoice.currency, currency),
    ):
        if value is not None:
            query = query.where(column == value)
    if period_start and period_end and period_start >= period_end:
        raise HTTPException(422, "Period end must follow start")
    if period_start:
        query = query.where(Invoice.period_end > period_start)
    if period_end:
        query = query.where(Invoice.period_start < period_end)
    if search:
        query = query.where(
            Invoice.project_name_snapshot.icontains(search, autoescape=True)
            | Invoice.invoice_number.icontains(search, autoescape=True)
        )
    order = {
        "newest": Invoice.created_at.desc(),
        "oldest": Invoice.created_at,
        "amount": Invoice.grand_total.desc(),
    }[sort]
    return response(
        [
            {**row(i), **service(request).totals(db, i)}
            for i in db.scalars(query.order_by(order, Invoice.id).limit(limit).offset(offset))
        ]
    )


@router.get("/api/v1/invoices/{identifier}")
def invoice_detail(identifier: UUID, request: Request, db: DB):
    invoice = get(request, db, Invoice, identifier)
    return response(
        {
            **row(invoice),
            **service(request).totals(db, invoice),
            "validation": BillingValidationService(service(request)).validate(db, invoice),
        }
    )


@router.get("/api/v1/invoices/{identifier}/lines")
def lines(identifier: UUID, request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    get(request, db, Invoice, identifier)
    return response(
        [
            row(v)
            for v in db.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == identifier)
                .order_by(InvoiceLine.meter_name, InvoiceLine.unit_price, InvoiceLine.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    )


@router.get("/api/v1/invoices/{identifier}/charges")
def sources(
    identifier: UUID,
    request: Request,
    db: DB,
    limit: Limit = 100,
    offset: Offset = 0,
    line_id: UUID | None = None,
):
    get(request, db, Invoice, identifier)
    query = (
        select(InvoiceChargeLink, ChargeRecord)
        .join(ChargeRecord, InvoiceChargeLink.charge_record_id == ChargeRecord.id)
        .where(InvoiceChargeLink.invoice_id == identifier)
    )
    if line_id:
        query = query.where(InvoiceChargeLink.invoice_line_id == line_id)
    return response(
        [
            {"link": row(link), "charge": row(charge)}
            for link, charge in db.execute(query.order_by(InvoiceChargeLink.id).limit(limit).offset(offset))
        ]
    )


@router.post("/api/v1/invoices/{identifier}/regenerate")
def regenerate(identifier: UUID, request: Request, actor: Operator):
    return mutate(request, lambda db, s: s.regenerate(db, s.get(db, Invoice, identifier), actor))


@router.post("/api/v1/invoices/{identifier}/review")
def review_invoice(identifier: UUID, request: Request, actor: Operator):
    return mutate(request, lambda db, s: s.review_invoice(db, s.get(db, Invoice, identifier), actor))


@router.post("/api/v1/invoices/{identifier}/finalize")
def finalize_invoice(identifier: UUID, request: Request, actor: Admin):
    return mutate(request, lambda db, s: s.finalize_invoice(db, s.get(db, Invoice, identifier), actor))


@router.get("/api/v1/invoices/{identifier}/adjustments")
def adjustments(identifier: UUID, request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    get(request, db, Invoice, identifier)
    return response(
        [
            row(v)
            for v in db.scalars(
                select(BillingAdjustment)
                .where(BillingAdjustment.invoice_id == identifier)
                .order_by(BillingAdjustment.created_at, BillingAdjustment.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    )


@router.get("/api/v1/adjustments")
def all_adjustments(request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    return response(
        [
            row(v)
            for v in db.scalars(
                select(BillingAdjustment)
                .join(Invoice)
                .where(Invoice.cloud_id == service(request).cloud)
                .order_by(BillingAdjustment.created_at.desc(), BillingAdjustment.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    )


@router.post("/api/v1/invoices/{identifier}/adjustments")
def adjustment_create(identifier: UUID, data: AdjustmentInput, request: Request, actor: Admin):
    return mutate(
        request, lambda db, s: s.adjustment(db, s.get(db, Invoice, identifier), data.model_dump(), actor)
    )


@router.post("/api/v1/adjustments/{identifier}/{action}")
def adjustment_action(identifier: UUID, action: Literal["approve", "apply"], request: Request, actor: Admin):
    return mutate(
        request,
        lambda db, s: s.adjustment_transition(db, s.get(db, BillingAdjustment, identifier), action, actor),
    )


@router.get("/api/v1/invoices/{identifier}/audit")
def invoice_audit(identifier: UUID, request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    get(request, db, Invoice, identifier)
    adjustment_ids = select(BillingAdjustment.id).where(BillingAdjustment.invoice_id == identifier)
    return response(
        [
            row(v)
            for v in db.scalars(
                select(BillingAudit)
                .where(
                    BillingAudit.cloud_id == service(request).cloud,
                    (BillingAudit.entity_id == identifier) | BillingAudit.entity_id.in_(adjustment_ids),
                )
                .order_by(BillingAudit.created_at, BillingAudit.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    )


def summary(request, db, cycle=None):
    s = service(request)
    query = select(Invoice).where(Invoice.cloud_id == s.cloud)
    if cycle:
        query = query.where(Invoice.billing_cycle_id == cycle.id)
    totals = {
        currency: amount
        for currency, amount in db.execute(
            query.with_only_columns(Invoice.currency, func.sum(Invoice.grand_total)).group_by(
                Invoice.currency
            )
        )
    }
    counts = {
        status: count
        for status, count in db.execute(
            query.with_only_columns(Invoice.status, func.count()).group_by(Invoice.status)
        )
    }
    result = dict(
        currency_totals=totals,
        status_counts=counts,
        projects=db.scalar(query.with_only_columns(func.count(func.distinct(Invoice.project_id)))),
    )
    if cycle:
        missing = incomplete_usage(
            db, s.cloud, cycle.usage_calculation_version, cycle.period_start, cycle.period_end
        )
        readiness = billing_readiness(
            db, s.cloud, cycle.usage_calculation_version, cycle.period_start, cycle.period_end
        )
        issues = []
        if readiness["provisional_periods"] or readiness["quality_periods"]:
            issues.append(
                dict(
                    code="METERING_DATA_INCOMPLETE",
                    severity="ERROR",
                    message="Cycle contains unresolved metering periods",
                )
            )
        for invoice in db.scalars(query.execution_options(yield_per=100)):
            issues.extend(
                {"invoice_id": str(invoice.id), **i}
                for i in BillingValidationService(s).validate(db, invoice)["issues"]
            )
        result.update(
            metering_readiness=readiness,
            billing_cycle=cycle.name,
            status=cycle.status,
            unrated_usage_count=missing,
            provisional_charge_count=db.scalar(
                s.eligible(cycle)
                .with_only_columns(func.count())
                .where(ChargeRecord.source_usage_status != "FINAL")
            ),
            blocking_issue_count=sum(i["severity"] == "ERROR" for i in issues) + bool(missing),
            issues=issues[:100],
            draft_invoices=counts.get("DRAFT", 0),
            finalized_invoices=counts.get("FINALIZED", 0),
        )
    return result


@router.get("/api/v1/billing/summary")
def billing_summary(
    request: Request, db: DB, start: AwareDatetime | None = None, end: AwareDatetime | None = None
):
    if start is not None or end is not None:
        from app.api.internal import internal_report

        result = internal_report(request, db, start, end)
        result.pop("projects")
        result.pop("instances")
        result.pop("trace")
        return response(result)
    return response(summary(request, db))


@router.get("/api/v1/billing/cycles/{identifier}/summary")
def cycle_summary(identifier: UUID, request: Request, db: DB):
    return response(summary(request, db, get(request, db, BillingCycle, identifier)))


@router.get("/api/v1/billing/quality")
def quality(request: Request, db: DB, limit: Limit = 50, offset: Offset = 0):
    cycles = list(
        db.scalars(
            select(BillingCycle)
            .where(BillingCycle.cloud_id == service(request).cloud)
            .order_by(BillingCycle.created_at.desc(), BillingCycle.id)
            .limit(limit)
            .offset(offset)
        )
    )
    conflicts = []
    for run in db.scalars(
        select(RatingRun)
        .where(RatingRun.cloud_id == service(request).cloud, RatingRun.status.in_(["PARTIAL", "FAILED"]))
        .order_by(RatingRun.created_at.desc())
        .limit(100)
    ):
        conflicts.extend(
            {"rating_run_id": str(run.id), **error}
            for error in run.errors
            if error.get("code") == "BILLED_CHARGE_CONFLICT"
        )
    return response(
        dict(
            cycles=[{"cycle_id": str(c.id), **summary(request, db, c)} for c in cycles],
            billed_charge_conflicts=conflicts,
        )
    )
