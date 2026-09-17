from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.billing.aggregate import cloud_summary, project_summaries
from app.models import Cloud, Instance, Project, SyncRun, Volume, utcnow
from app.schemas import (
    CloudOut,
    CurrentSummary,
    InstanceOut,
    Page,
    ProjectOut,
    ProjectSummary,
    RunOut,
    VolumeOut,
)
from app.sync.engine import SyncBusy

router = APIRouter(prefix="/api/v1")


def database(request: Request):
    with request.app.state.sessions() as db:
        yield db


DB = Annotated[Session, Depends(database)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


def cloud_id(request):
    return request.app.state.settings.openstack_cloud_id


def require_project(db, request, project_id):
    project = db.get(Project, (cloud_id(request), project_id))
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/health")
def health(request: Request):
    result = {"application": "OK", "database": "FAILED", "openstack": "NOT_CHECKED"}
    try:
        with request.app.state.sessions() as db:
            db.execute(text("SELECT 1"))
            result["database"] = "OK"
            cloud = db.get(Cloud, cloud_id(request))
            if cloud:
                result.update(
                    cloud_name=cloud.name,
                    openstack=cloud.connection_status,
                    region=cloud.region,
                    last_successful_sync=cloud.last_successful_sync,
                    last_failed_sync=cloud.last_failed_sync,
                    last_attempt_at=cloud.last_attempt_at,
                    service_status=cloud.service_status,
                )
            result["projects_discovered"] = db.scalar(
                select(func.count())
                .select_from(Project)
                .where(
                    Project.cloud_id == cloud_id(request),
                    Project.is_placeholder.is_(False),
                    Project.is_missing.is_(False),
                )
            )
            result["sync_running"] = (
                db.scalar(
                    select(func.count())
                    .select_from(SyncRun)
                    .where(SyncRun.cloud_id == cloud_id(request), SyncRun.status == "RUNNING")
                )
                > 0
            )
    except Exception:
        return JSONResponse(
            status_code=503, content={"application": "OK", "database": "FAILED", "openstack": "UNKNOWN"}
        )
    from datetime import UTC, datetime

    from fastapi.encoders import jsonable_encoder

    result["checked_at"] = utcnow()
    return JSONResponse(
        content=jsonable_encoder(
            result,
            custom_encoder={
                datetime: lambda dt: dt.replace(tzinfo=dt.tzinfo or UTC).astimezone(UTC).isoformat()
            },
        )
    )


@router.get("/readiness")
def readiness(db: DB):
    try:
        version = db.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0005":
            raise ValueError()
        return {"ready": True, "database": "OK"}
    except Exception:
        return JSONResponse(
            status_code=503, content={"ready": False, "database": "UNAVAILABLE_OR_UNMIGRATED"}
        )


@router.get("/cloud", response_model=CloudOut)
def get_cloud(request: Request, db: DB):
    cloud = db.get(Cloud, cloud_id(request))
    if not cloud:
        raise HTTPException(503, "Cloud is initializing")
    return cloud


@router.post("/sync", status_code=202)
def manual_sync(request: Request):
    # Require JSON: cross-origin form posts must not trigger a sync on this no-login POC.
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Use Content-Type: application/json")
    try:
        run_id = request.app.state.sync_manager.trigger()
        return {"sync_run_id": run_id, "status": "RUNNING"}
    except SyncBusy:
        raise HTTPException(409, "A synchronization is already running") from None


@router.get("/sync-runs", response_model=Page[RunOut])
def sync_runs(request: Request, db: DB, limit: Limit = 50, offset: Offset = 0):
    query = select(SyncRun).where(SyncRun.cloud_id == cloud_id(request))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(SyncRun.started_at.desc(), SyncRun.sync_run_id).offset(offset).limit(limit)
    ).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/projects", response_model=Page[ProjectOut])
def projects(
    request: Request,
    db: DB,
    q: Annotated[str, Query(max_length=255)] = "",
    limit: Limit = 50,
    offset: Offset = 0,
):
    query = select(Project).where(Project.cloud_id == cloud_id(request))
    if q:
        conditions = [Project.project_name.icontains(q, autoescape=True)]
        try:
            conditions.append(Project.project_id == UUID(q))
        except ValueError:
            pass
        query = query.where(or_(*conditions))
    return {
        "total": db.scalar(select(func.count()).select_from(query.subquery())),
        "items": db.scalars(
            query.order_by(Project.project_name, Project.project_id).limit(limit).offset(offset)
        ).all(),
        "limit": limit,
        "offset": offset,
    }


@router.get("/projects/{project_id}", response_model=ProjectOut)
def project_detail(project_id: UUID, request: Request, db: DB):
    return require_project(db, request, project_id)


def resource_page(db, request, project_id, model, id_column, limit, offset):
    require_project(db, request, project_id)
    query = select(model).where(model.cloud_id == cloud_id(request), model.project_id == project_id)
    return {
        "items": db.scalars(query.order_by(id_column).limit(limit).offset(offset)).all(),
        "total": db.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.get("/projects/{project_id}/instances", response_model=Page[InstanceOut])
def instances(project_id: UUID, request: Request, db: DB, limit: Limit = 50, offset: Offset = 0):
    return resource_page(db, request, project_id, Instance, Instance.instance_id, limit, offset)


@router.get("/projects/{project_id}/volumes", response_model=Page[VolumeOut])
def volumes(project_id: UUID, request: Request, db: DB, limit: Limit = 50, offset: Offset = 0):
    return resource_page(db, request, project_id, Volume, Volume.volume_id, limit, offset)


def summaries(db, request):
    return project_summaries(db, cloud_id(request), request.app.state.policy)


@router.get("/billing/current", response_model=CurrentSummary)
def current_billing(request: Request, db: DB):
    return cloud_summary(summaries(db, request))


Sort = Literal[
    "project_name",
    "project_id",
    "instance_count",
    "active_vm_count",
    "total_cost",
    "vcpu_count",
    "ram_gb",
    "nova_root_disk_gb",
    "nova_ephemeral_disk_gb",
    "cinder_volume_count",
    "cinder_volume_gb",
]


@router.get("/billing/projects", response_model=Page[ProjectSummary])
def billing_projects(
    request: Request,
    db: DB,
    q: Annotated[str, Query(max_length=255)] = "",
    sort: Sort = "project_name",
    direction: Literal["asc", "desc"] = "asc",
    limit: Limit = 50,
    offset: Offset = 0,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
):
    if start is not None or end is not None:
        from app.api.internal import internal_report
        from app.api.pricing import response

        result = internal_report(request, db, start, end)
        rows = result.pop("projects")
        result.pop("instances")
        result.pop("trace")
        rows = [
            {**r["current"], **r}
            for r in rows
            if not q or q.casefold() in (r["project_name"] + str(r["project_id"])).casefold()
        ]
        rows.sort(key=lambda r: str(r["project_id"]))
        rows.sort(
            key=lambda r: str(r.get(sort, "")).casefold()
            if sort.startswith("project_")
            else r["cost"]["total"]
            if sort == "total_cost"
            else r.get(sort, 0),
            reverse=direction == "desc",
        )
        return response(
            {
                **result,
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            }
        )
    if sort in ("active_vm_count", "total_cost"):
        raise HTTPException(422, "This sort requires start and end")
    rows = summaries(db, request)
    if q:
        rows = [
            row
            for row in rows
            if q.casefold() in row["project_name"].casefold()
            or q.casefold().replace("-", "") in str(row["project_id"]).replace("-", "")
        ]
    rows.sort(key=lambda row: str(row["project_id"]))
    rows.sort(
        key=lambda row: str(row[sort]).casefold() if sort.startswith("project_") else row[sort],
        reverse=direction == "desc",
    )
    return {"items": rows[offset : offset + limit], "total": len(rows), "limit": limit, "offset": offset}


@router.get("/billing/projects/{project_id}", response_model=ProjectSummary)
def project_billing(
    project_id: UUID,
    request: Request,
    db: DB,
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
):
    require_project(db, request, project_id)
    if start is not None or end is not None:
        from app.api.internal import internal_report
        from app.api.pricing import response

        result = internal_report(request, db, start, end, project_id)
        project = result.pop("projects")[0]
        result.pop("instances")
        result.pop("trace")
        return response({**result, **project, **project["current"]})
    return next(row for row in summaries(db, request) if row["project_id"] == project_id)


@router.get("/billing/policy")
def billing_policy(request: Request):
    return request.app.state.policy.model_dump()
