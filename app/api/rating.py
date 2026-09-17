from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime
from sqlalchemy import exists, func, select

from app.api.metering import Range, RunRequest
from app.api.pricing import Admin, response, row
from app.api.routes import DB, Limit, Offset
from app.core.jobs import JobBusy
from app.metering.math import quantity, rounded, seconds, utc
from app.models import (
    ChargeRecord,
    PriceBookVersion,
    ProjectAssignment,
    ProjectOverride,
    RatingRun,
    UsageRecord,
)
from app.pricing.service import intersects
from app.rating.query import charge_query, clipped, summarize

router = APIRouter(prefix="/api/v1/rating", tags=["Rated monetary charges"])


def filters(
    request: Request,
    start: AwareDatetime,
    end: AwareDatetime,
    project_id: UUID | None = None,
    resource_id: UUID | None = None,
    meter: str | None = None,
    product: UUID | None = None,
    status: Literal["RATED", "UNRATED", "SUPERSEDED"] | None = None,
    currency: Literal["VND", "USD"] | None = None,
    timezone: str | None = None,
):
    try:
        Range(start=start, end=end)
        ZoneInfo(timezone or request.app.state.settings.billing_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Use a positive aware range and valid IANA timezone") from None
    return dict(
        cloud=request.app.state.settings.openstack_cloud_id,
        usage_version=request.app.state.settings.metering_calculation_version,
        start=utc(start),
        end=utc(end),
        project_id=project_id,
        resource_id=resource_id,
        meter=meter,
        product=product,
        status=status,
        currency=currency,
        timezone=timezone or request.app.state.settings.billing_timezone,
    )


Filters = Annotated[dict, Depends(filters)]


@router.post("/run")
def run_rating(body: RunRequest, request: Request, actor: Admin):
    try:
        return response(row(request.app.state.rating.run(body.start, body.end, body.force, actor)))
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/summary")
def summary(request: Request, db: DB, options: Filters):
    last = db.scalars(
        select(RatingRun)
        .where(RatingRun.cloud_id == options["cloud"])
        .order_by(RatingRun.started_at.desc())
        .limit(1)
    ).first()
    return response(
        {
            "period_start": options["start"],
            "period_end": options["end"],
            "timezone": options["timezone"],
            "source_usage_status": "FINAL",
            "usage_calculation_version": options["usage_version"],
            "currencies": summarize(db, options),
            "last_rating_run": row(last) if last else None,
        }
    )


@router.get("/projects")
def projects(db: DB, options: Filters, limit: Limit = 100, offset: Offset = 0):
    items = [p for bucket in summarize(db, options) for p in bucket["projects"]]
    return response(
        {"items": items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}
    )


@router.get("/projects/{project_id}")
def project_cost(project_id: UUID, db: DB, options: Filters):
    return response(
        {
            "project_id": project_id,
            "source_usage_status": "FINAL",
            "currencies": summarize(db, {**options, "project_id": project_id}),
        }
    )


@router.get("/charges")
def charges(db: DB, options: Filters, limit: Limit = 100, offset: Offset = 0):
    query = charge_query(**options)
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    items = []
    for record in db.scalars(
        query.order_by(ChargeRecord.rated_period_start, ChargeRecord.id).limit(limit).offset(offset)
    ):
        start, end, subtotal = clipped(record, options["start"], options["end"])
        items.append(
            {
                **row(record),
                "query_period_start": start,
                "query_period_end": end,
                "query_subtotal": subtotal,
                "query_rated_quantity": rounded(quantity(record.allocated_quantity, start, end)),
                "query_duration_seconds": seconds(start, end),
            }
        )
    return response({"items": items, "total": count, "limit": limit, "offset": offset})


@router.get("/charges/{identity}")
def charge_detail(identity: UUID, request: Request, db: DB):
    charge = db.get(ChargeRecord, identity)
    if charge is None or charge.cloud_id != request.app.state.settings.openstack_cloud_id:
        raise HTTPException(404, "Charge not found")
    usage = db.get(UsageRecord, charge.usage_record_id)
    return response({**row(charge), "original_usage": row(usage)})


@router.get("/runs")
def runs(request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    query = select(RatingRun).where(RatingRun.cloud_id == request.app.state.settings.openstack_cloud_id)
    return response(
        {
            "items": [
                row(r)
                for r in db.scalars(query.order_by(RatingRun.started_at.desc()).limit(limit).offset(offset))
            ],
            "total": db.scalar(select(func.count()).select_from(query.subquery())),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/quality")
def quality(request: Request, db: DB):
    cloud = request.app.state.settings.openstack_cloud_id
    base = (
        select(ChargeRecord)
        .join(UsageRecord, UsageRecord.usage_record_id == ChargeRecord.usage_record_id)
        .where(
            ChargeRecord.cloud_id == cloud,
            UsageRecord.calculation_version == request.app.state.settings.metering_calculation_version,
        )
    )
    counts = dict(
        db.execute(
            select(ChargeRecord.status, func.count())
            .where(ChargeRecord.cloud_id == cloud)
            .group_by(ChargeRecord.status)
        ).all()
    )
    reasons = dict(
        db.execute(
            select(ChargeRecord.unrated_reason, func.count())
            .where(ChargeRecord.cloud_id == cloud, ChargeRecord.status == "UNRATED")
            .group_by(ChargeRecord.unrated_reason)
        ).all()
    )
    unrated = base.where(ChargeRecord.status == "UNRATED")
    pending = (
        select(func.count())
        .select_from(UsageRecord)
        .where(
            UsageRecord.cloud_id == cloud,
            UsageRecord.status == "FINAL",
            UsageRecord.calculation_version == request.app.state.settings.metering_calculation_version,
            ~exists(
                select(ChargeRecord.id).where(
                    ChargeRecord.usage_record_id == UsageRecord.usage_record_id,
                    ChargeRecord.status != "SUPERSEDED",
                )
            ),
        )
    )
    # Configuration is small and cached by rating; also detect corruption in portable development DBs.
    overlaps = 0
    for model, conditions, keys in (
        (PriceBookVersion, (PriceBookVersion.status == "ACTIVE",), ("price_book_id",)),
        (
            ProjectAssignment,
            (ProjectAssignment.cloud_id == cloud, ProjectAssignment.retired_at.is_(None)),
            ("cloud_id", "project_id"),
        ),
        (
            ProjectOverride,
            (ProjectOverride.cloud_id == cloud, ProjectOverride.retired_at.is_(None)),
            ("cloud_id", "project_id", "product_id"),
        ),
    ):
        rows = list(db.scalars(select(model).where(*conditions)))
        overlaps += sum(
            all(getattr(a, k) == getattr(b, k) for k in keys) and intersects(a, b)
            for i, a in enumerate(rows)
            for b in rows[i + 1 :]
        )
    return response(
        {
            "status_counts": counts,
            "unrated_reasons": reasons,
            "pending_final_usage": db.scalar(pending),
            "unrated_usage_records": db.scalar(
                select(func.count(func.distinct(ChargeRecord.usage_record_id))).where(
                    ChargeRecord.cloud_id == cloud, ChargeRecord.status == "UNRATED"
                )
            ),
            "provisional_charges": 0,
            "projects_without_price_book": db.scalar(
                select(func.count(func.distinct(ChargeRecord.project_id))).where(
                    ChargeRecord.cloud_id == cloud,
                    ChargeRecord.status == "UNRATED",
                    ChargeRecord.unrated_reason == "NO_PRICE_BOOK",
                )
            ),
            "unknown_meter_products": list(
                db.scalars(
                    select(ChargeRecord.meter_name)
                    .where(
                        ChargeRecord.cloud_id == cloud,
                        ChargeRecord.status == "UNRATED",
                        ChargeRecord.unrated_reason == "NO_PRODUCT",
                    )
                    .distinct()
                )
            ),
            "price_gap_segments": reasons.get("PRICE_GAP", 0),
            "rating_failures": db.scalar(
                select(func.count())
                .select_from(RatingRun)
                .where(RatingRun.cloud_id == cloud, RatingRun.status.in_(["FAILED", "PARTIAL"]))
            ),
            "source_usage_policy": "Only FINAL persisted usage is rated; open usage is excluded",
            "overlapping_configuration": overlaps,
            "sample_unrated": [row(r) for r in db.scalars(unrated.limit(25))],
            "recent_failed_runs": [
                row(r)
                for r in db.scalars(
                    select(RatingRun)
                    .where(RatingRun.cloud_id == cloud, RatingRun.status.in_(["FAILED", "PARTIAL"]))
                    .order_by(RatingRun.started_at.desc())
                    .limit(20)
                )
            ],
        }
    )
