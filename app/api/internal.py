from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime

from app.api.metering import Range
from app.api.pricing import response
from app.api.routes import DB, Limit, Offset
from app.billing.internal import report
from app.models import utcnow
from app.openstack.diagnostics import diagnostics

router = APIRouter(tags=["Internal billing"])


def internal_report(request, db, start=None, end=None, project_id=None, instance_id=None, trace=False):
    settings = request.app.state.settings
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both start and end")
    if start is None:
        now = utcnow().astimezone(ZoneInfo(settings.billing_timezone))
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (
            datetime(now.year + 1, 1, 1, tzinfo=now.tzinfo)
            if now.month == 12
            else datetime(now.year, now.month + 1, 1, tzinfo=now.tzinfo)
        )
    if start >= end or (end - start) > timedelta(days=3660):
        raise HTTPException(422, "Use a positive range of at most 3660 days")
    Range(start=start, end=end)
    as_of = request.query_params.get("as_of")
    try:
        as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
        if as_of and as_of.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "as_of must include a UTC offset") from None
    if as_of:
        as_of = min(as_of, utcnow())
    try:
        result = report(db, settings, start, end, project_id, instance_id, now=as_of, trace=trace)
        connection = request.app.state.notifications.status
        result["diagnostics"]["notification_connection"] = connection
        if settings.nova_notification_enabled and connection != "CONNECTED":
            result["cost_complete"] = False
            if result["data_quality_status"] == "HEALTHY":
                result["data_quality_status"] = "PARTIAL"
        return result
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/v1/diagnostics/openstack")
def openstack_diagnostics(request: Request, db: DB):
    result = diagnostics(db, request.app.state.settings)
    result["notification_connection"] = request.app.state.notifications.status
    if (
        request.app.state.settings.nova_notification_enabled
        and result["notification_connection"] != "CONNECTED"
        and result["data_quality_status"] == "HEALTHY"
    ):
        result["data_quality_status"] = "PARTIAL"
    return response(result)


@router.get("/api/v1/billing/instances")
def instance_costs(
    request: Request,
    db: DB,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    project_id: UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    result = internal_report(request, db, start, end, project_id)
    items = result.pop("instances")
    result.pop("projects")
    result.pop("trace")
    return response(
        {
            **result,
            "items": items[offset : offset + limit],
            "total": len(items),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/v1/billing/instances/{instance_id}")
def instance_cost(
    instance_id: UUID,
    request: Request,
    db: DB,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    result = internal_report(request, db, start, end, instance_id=instance_id, trace=True)
    instance = result.pop("instances")[0]
    result.pop("projects")
    trace = result.pop("trace")
    return response(
        {
            **result,
            **instance,
            "trace": trace[offset : offset + limit],
            "trace_total": len(trace),
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/v1/diagnostics/notifications")
def notification_events(request: Request, db: DB, limit: Limit = 50, offset: Offset = 0):
    from sqlalchemy import select

    from app.models import ProcessedNotification

    rows = db.scalars(
        select(ProcessedNotification)
        .where(ProcessedNotification.cloud_id == request.app.state.settings.openstack_cloud_id)
        .order_by(ProcessedNotification.received_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return response(
        {
            "connection": request.app.state.notifications.status,
            "items": [{c.key: getattr(row, c.key) for c in row.__table__.columns} for row in rows],
        }
    )
