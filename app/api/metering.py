from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from sqlalchemy import func, or_, select

from app.api.routes import DB, Limit, Offset
from app.metering.engine import MeteringBusy, PolicyVersionConflict
from app.metering.math import rounded, utc
from app.metering.query import (
    daily_totals,
    group_projects,
    group_resources,
    scoped_periods,
    totals,
    usage_view,
)
from app.metering.registry import BY_NAME, METERS
from app.models import (
    Instance,
    MeteringPolicyVersion,
    MeteringRun,
    Observation,
    StatePeriod,
    SyncRun,
    UsageRecord,
    Volume,
)
from app.schemas import ReadModel

router = APIRouter(prefix="/api/v1/metering", tags=["Historical metering"])
ResourceType = Literal["INSTANCE", "VOLUME"]


def response(value):
    return JSONResponse(
        content=jsonable_encoder(
            value,
            custom_encoder={
                Decimal: lambda d: format(rounded(d), "f"),
                datetime: lambda d: utc(d).isoformat(),
                UUID: str,
            },
        )
    )


def public_row(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


class Range(BaseModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def bounds(self):
        if self.start >= self.end or self.end - self.start > timedelta(days=3660):
            raise ValueError("Range must be positive and at most 3660 days; use [start, end)")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    force: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if (self.start is None) != (self.end is None) or self.force and self.start is None:
            raise ValueError("Explicit replay requires both start and end")
        if self.start:
            Range(start=self.start, end=self.end)
        return self


class UsageRecordOut(ReadModel):
    usage_record_id: UUID
    cloud_id: UUID
    project_id: UUID
    resource_type: ResourceType
    resource_id: UUID
    meter_name: str
    unit: str
    period_start: datetime
    period_end: datetime
    duration_seconds: Decimal
    allocated_quantity: Decimal
    usage_quantity: Decimal
    source_state_period_id: UUID
    calculation_version: str
    metering_run_id: UUID
    status: Literal["FINAL"]
    created_at: datetime


def filters(
    request: Request,
    start: AwareDatetime,
    end: AwareDatetime,
    project_id: UUID | None = None,
    resource_type: ResourceType | None = None,
    resource_id: UUID | None = None,
    meter: str | None = None,
    cloud_id: UUID | None = None,
    calculation_version: Annotated[str | None, Query(pattern=r"^meter-v[0-9]+$", max_length=40)] = None,
    timezone: str | None = None,
):
    try:
        bounds = Range(start=start, end=end)
        ZoneInfo(timezone or request.app.state.settings.billing_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(
            422, "Use an increasing offset-aware range (at most 3660 days) and valid IANA timezone"
        ) from None
    if meter and meter not in BY_NAME:
        raise HTTPException(422, "Unknown meter")
    if resource_id and resource_type is None:
        raise HTTPException(422, "resource_type is required with resource_id")
    return {
        "cloud_id": cloud_id or request.app.state.settings.openstack_cloud_id,
        "version": calculation_version or request.app.state.settings.metering_calculation_version,
        "start": utc(bounds.start),
        "end": utc(bounds.end),
        "project_id": project_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "meter": meter,
        "timezone": timezone or request.app.state.settings.billing_timezone,
    }


Filters = Annotated[dict, Depends(filters)]


def read_usage(db, filters):
    options = {key: value for key, value in filters.items() if key != "timezone"}
    try:
        return usage_view(db, **options)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


def envelope(filters, cutoff, issues):
    return {
        "cloud_id": filters["cloud_id"],
        "calculation_version": filters["version"],
        "period_start": filters["start"],
        "period_end": filters["end"],
        "effective_end": cutoff,
        "timezone": filters["timezone"],
        "interval_semantics": "[start, end)",
        "quality_issues": issues,
        "precision": "Polling observations; baseline excludes pre-discovery allocation history",
    }


@router.get("/summary")
def summary(db: DB, options: Filters):
    rows, issues, cutoff = read_usage(db, options)
    return response(
        {
            **envelope(options, cutoff, issues),
            **totals(rows),
            "daily": daily_totals(rows, options["timezone"]),
        }
    )


@router.get("/projects")
def projects(db: DB, options: Filters, limit: Limit = 50, offset: Offset = 0):
    rows, issues, cutoff = read_usage(db, options)
    grouped = group_projects(db, options["cloud_id"], rows)
    return response(
        {
            **envelope(options, cutoff, issues),
            "items": grouped[offset : offset + limit],
            "total": len(grouped),
            "offset": offset,
            "limit": limit,
        }
    )


@router.get("/projects/{project_id}")
def project_summary(project_id: UUID, db: DB, options: Filters):
    options["project_id"] = project_id
    rows, issues, cutoff = read_usage(db, options)
    return response(
        {
            **envelope(options, cutoff, issues),
            "project_id": project_id,
            **totals(rows),
            "daily": daily_totals(rows, options["timezone"]),
        }
    )


@router.get("/projects/{project_id}/resources")
def resources(project_id: UUID, db: DB, options: Filters, limit: Limit = 50, offset: Offset = 0):
    options["project_id"] = project_id
    rows, issues, cutoff = read_usage(db, options)
    grouped = group_resources(rows)
    return response(
        {
            **envelope(options, cutoff, issues),
            "items": grouped[offset : offset + limit],
            "total": len(grouped),
            "offset": offset,
            "limit": limit,
        }
    )


@router.get("/resources/{resource_type}/{resource_id}/usage")
@router.get("/usage")
def usage(db: DB, options: Filters, limit: Limit = 50, offset: Offset = 0):
    # FastAPI binds resource_type/resource_id path parameters into the shared dependency.
    rows, issues, cutoff = read_usage(db, options)
    rows.sort(key=lambda row: (row["period_start"], str(row["resource_id"]), row["meter_name"]))
    return response(
        {
            **envelope(options, cutoff, issues),
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
        }
    )


@router.get("/records/{usage_record_id}", response_model=UsageRecordOut)
def usage_record(usage_record_id: UUID, db: DB, request: Request):
    row = db.get(UsageRecord, usage_record_id)
    if row is None or row.cloud_id != request.app.state.settings.openstack_cloud_id:
        raise HTTPException(404, "Usage record not found")
    return row


@router.get("/resources/{resource_type}/{resource_id}")
def resource(resource_type: ResourceType, resource_id: UUID, db: DB, request: Request):
    cloud = request.app.state.settings.openstack_cloud_id
    period = db.scalars(
        scoped_periods(cloud, resource_type=resource_type, resource_id=resource_id)
        .order_by(StatePeriod.valid_from.desc())
        .limit(1)
    ).first()
    if period is None:
        raise HTTPException(404, "No lifecycle history for this resource")
    return response(
        {
            "cloud_id": cloud,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "latest_period": public_row(period),
        }
    )


@router.get("/resources/{resource_type}/{resource_id}/lifecycle")
def lifecycle(
    resource_type: ResourceType,
    resource_id: UUID,
    db: DB,
    request: Request,
    limit: Limit = 50,
    offset: Offset = 0,
):
    query = scoped_periods(
        request.app.state.settings.openstack_cloud_id, resource_type=resource_type, resource_id=resource_id
    )
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(StatePeriod.valid_from).offset(offset).limit(limit))
    return response(
        {"items": [public_row(row) for row in rows], "total": count, "offset": offset, "limit": limit}
    )


@router.get("/observations/{observation_id}")
def observation(observation_id: UUID, db: DB, request: Request):
    row = db.get(Observation, observation_id)
    if row is None or row.cloud_id != request.app.state.settings.openstack_cloud_id:
        raise HTTPException(404, "Observation not found")
    return response(public_row(row))  # normalized allowlist only, never an SDK wire body


@router.get("/runs")
def runs(db: DB, request: Request, limit: Limit = 50, offset: Offset = 0):
    query = select(MeteringRun).where(MeteringRun.cloud_id == request.app.state.settings.openstack_cloud_id)
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(MeteringRun.started_at.desc()).offset(offset).limit(limit))
    return response(
        {"items": [public_row(row) for row in rows], "total": count, "offset": offset, "limit": limit}
    )


@router.post("/run")
def run(body: RunRequest, request: Request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Use Content-Type: application/json")
    try:
        result = request.app.state.metering.run(**body.model_dump())
    except MeteringBusy:
        raise HTTPException(409, "A sync or metering operation already owns this cloud") from None
    except PolicyVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return response(public_row(result))


@router.get("/meters")
def meters(request: Request, db: DB):
    versions = db.scalars(
        select(MeteringPolicyVersion).where(
            MeteringPolicyVersion.cloud_id == request.app.state.settings.openstack_cloud_id
        )
    )
    zone = ZoneInfo(request.app.state.settings.billing_timezone)
    today = datetime.now(zone).date()
    return response(
        {
            "default_start_date": today.replace(day=1).isoformat(),
            "default_end_date": (today + timedelta(days=1)).isoformat(),
            "meters": [asdict(meter) for meter in METERS],
            "default_timezone": request.app.state.settings.billing_timezone,
            "current_version": request.app.state.settings.metering_calculation_version,
            "versions": [public_row(version) for version in versions],
        }
    )


@router.get("/calendar-range")
def calendar_range(request: Request, start_date: date, end_date: date, timezone: str | None = None):
    try:
        zone = ZoneInfo(timezone or request.app.state.settings.billing_timezone)
        bounds = Range(
            start=datetime.combine(start_date, time.min, tzinfo=zone),
            end=datetime.combine(end_date, time.min, tzinfo=zone),
        )
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Invalid calendar range or IANA timezone") from None
    return response({"start": bounds.start, "end": bounds.end, "timezone": str(zone)})


@router.get("/quality")
def quality(request: Request, db: DB):
    cloud, policy = request.app.state.settings.openstack_cloud_id, request.app.state.metering.policy
    result = {}
    for model, kind, known in (
        (Instance, "instances", list(policy.instance)),
        (Volume, "volumes", policy.volume.counted_states + policy.volume.excluded_states),
    ):
        result[f"unknown_{kind}_states"] = [
            {"state": state, "count": count}
            for state, count in db.execute(
                select(model.status, func.count())
                .where(model.cloud_id == cloud, model.status.not_in(known))
                .group_by(model.status)
            )
        ]
        result[f"missing_{kind}"] = db.scalar(
            select(func.count()).select_from(model).where(model.cloud_id == cloud, model.is_missing.is_(True))
        )
        result[f"pending_{kind}"] = db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.cloud_id == cloud, model.missing_scans > 0, model.is_missing.is_(False))
        )
    result["sync_status_counts"] = dict(
        db.execute(
            select(SyncRun.status, func.count()).where(SyncRun.cloud_id == cloud).group_by(SyncRun.status)
        ).all()
    )
    result["metering_status_counts"] = dict(
        db.execute(
            select(MeteringRun.status, func.count())
            .where(MeteringRun.cloud_id == cloud)
            .group_by(MeteringRun.status)
        ).all()
    )
    result["recent_metering_errors"] = [
        public_row(row)
        for row in db.scalars(
            select(MeteringRun)
            .where(MeteringRun.cloud_id == cloud, MeteringRun.status.in_(["FAILED", "PARTIAL"]))
            .order_by(MeteringRun.started_at.desc())
            .limit(20)
        )
    ]
    result["recent_sync_errors"] = [
        public_row(row)
        for row in db.scalars(
            select(SyncRun)
            .where(SyncRun.cloud_id == cloud, SyncRun.status.in_(["FAILED", "PARTIAL"]))
            .order_by(SyncRun.started_at.desc())
            .limit(20)
        )
    ]
    windows = (
        select(
            StatePeriod.valid_to,
            func.lead(StatePeriod.valid_from)
            .over(
                partition_by=(StatePeriod.cloud_id, StatePeriod.resource_type, StatePeriod.resource_id),
                order_by=(StatePeriod.valid_from, StatePeriod.period_id),
            )
            .label("next_from"),
        )
        .where(StatePeriod.cloud_id == cloud)
        .subquery()
    )
    result["lifecycle_inconsistencies"] = db.scalar(
        select(func.count())
        .select_from(windows)
        .where(
            windows.c.next_from.is_not(None),
            or_(windows.c.valid_to.is_(None), windows.c.valid_to > windows.c.next_from),
        )
    )
    return response(result)
