"""Read-only reconciliation from the last fully exhausted SDK service responses."""

from collections import Counter

from sqlalchemy import func, select

from app.metering.math import utc
from app.metering.policy import load_metering_policy
from app.models import Cloud, Instance, Project, SyncRun, Volume, utcnow


def diagnostics(db, settings):
    cloud = settings.openstack_cloud_id
    connection = db.get(Cloud, cloud)
    run = db.scalars(
        select(SyncRun)
        .where(SyncRun.cloud_id == cloud, SyncRun.status != "RUNNING")
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()
    projects = list(db.scalars(select(Project).where(Project.cloud_id == cloud)))
    instances = list(db.scalars(select(Instance).where(Instance.cloud_id == cloud)))
    volumes = list(db.scalars(select(Volume).where(Volume.cloud_id == cloud)))
    services = run.services if run else {}
    successful = sum(services.get(s, {}).get("status") == "SUCCESS" for s in ("keystone", "nova", "cinder"))
    status = "HEALTHY" if successful == 3 else "PARTIAL" if successful else "INCOMPLETE"
    stale = (
        not connection
        or not connection.last_attempt_at
        or (utcnow() - utc(connection.last_attempt_at)).total_seconds()
        > max(3 * settings.sync_interval_seconds, 60)
    )
    policy = load_metering_policy(settings.metering_policy_path)
    placeholders = {p.project_id for p in projects if p.is_placeholder or p.is_missing}
    unknown = sum(r.project_id in placeholders for r in instances + volumes if not r.is_missing)
    unknown_states = sum(r.status not in policy.instance for r in instances if not r.is_missing)
    pending = sum(bool(r.missing_scans) and not r.is_missing for r in instances + volumes)
    warnings = sum(bool(r.quality_issues) for r in instances + volumes if not r.is_missing)
    if status == "HEALTHY" and (stale or unknown or unknown_states or pending or warnings):
        status = "PARTIAL"
    live_instances = [
        r
        for r in instances
        if not r.is_missing and not r.deleted_at_openstack and r.status not in ("DELETED", "SOFT_DELETED")
    ]
    live_volumes = [r for r in volumes if not r.is_missing]
    vm_counts = Counter(r.project_id for r in live_instances)
    volume_counts = Counter(r.project_id for r in live_volumes)

    def observed(service, pid):
        info = services.get(service, {})
        return (
            info.get("per_project", {}).get(str(pid), 0)
            if info.get("status") in ("SUCCESS", "PARTIAL")
            else None
        )

    from app.models import ProcessedNotification

    notification_issues = db.scalar(
        select(func.count())
        .select_from(ProcessedNotification)
        .where(ProcessedNotification.cloud_id == cloud, ProcessedNotification.issue_code.is_not(None))
    )
    if notification_issues and status == "HEALTHY":
        status = "PARTIAL"
    return dict(
        notification_issue_count=notification_issues,
        notifications_enabled=settings.nova_notification_enabled,
        data_quality_status=status,
        auth_ok=bool(connection and connection.connection_status == "CONNECTED"),
        region=settings.os_region_name,
        interface=settings.os_interface,
        last_sync=run.finished_at if run else None,
        stale=stale,
        services=services,
        scope_requested="all_projects",
        scope_validation="CLI comparison required; successful requests do not prove policy visibility",
        projects_visible=services.get("keystone", {}).get("discovered"),
        instances_visible=services.get("nova", {}).get("discovered"),
        volumes_visible=services.get("cinder", {}).get("discovered"),
        billing_projects=sum(not p.is_missing and not p.is_placeholder for p in projects),
        billing_instances=len(live_instances),
        billing_volumes=len(live_volumes),
        projects_with_vms=sum(
            vm_counts[p.project_id] > 0 for p in projects if not p.is_placeholder and not p.is_missing
        ),
        projects_without_vms=sum(
            vm_counts[p.project_id] == 0 for p in projects if not p.is_placeholder and not p.is_missing
        ),
        unknown_project_resources=unknown,
        unknown_instance_states=unknown_states,
        quality_warning_resources=warnings,
        pending_delete_confirmation=pending,
        missing_resources=sum(r.is_missing for r in instances + volumes),
        api_failures=run.errors if run else [{"code": "NOT_CHECKED"}],
        per_project=[
            dict(
                project_id=p.project_id,
                project_name=p.project_name,
                is_placeholder=p.is_placeholder,
                openstack_vms=observed("nova", p.project_id),
                billing_vms=vm_counts[p.project_id],
                openstack_volumes=observed("cinder", p.project_id),
                billing_volumes=volume_counts[p.project_id],
            )
            for p in projects
        ],
    )
