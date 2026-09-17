from decimal import Decimal

from sqlalchemy import case, func, select

from app.models import Instance, Project, Volume

METRICS = (
    "instance_count",
    "vcpu_count",
    "ram_gb",
    "nova_root_disk_gb",
    "nova_ephemeral_disk_gb",
    "cinder_volume_count",
    "cinder_volume_gb",
    "incomplete_instances",
    "incomplete_volumes",
    "pending_missing_instances",
    "pending_missing_volumes",
)


def project_summaries(db, cloud_id, policy):
    """Aggregate each inventory separately: joining VMs to volumes multiplies totals."""
    summaries = {
        row.project_id: {
            "project_id": row.project_id,
            "project_name": row.project_name,
            "enabled": row.enabled,
            "is_placeholder": row.is_placeholder,
            "is_missing": row.is_missing,
            **dict.fromkeys(METRICS, 0),
        }
        for row in db.scalars(select(Project).where(Project.cloud_id == cloud_id))
    }

    def dimension(column, dimension_name):
        excluded = [
            state for state, rule in policy.nova.state_metrics.items() if not getattr(rule, dimension_name)
        ]
        value = case((Instance.status.in_(excluded), 0), else_=column) if excluded else column
        return func.coalesce(func.sum(value), 0)

    instances = db.execute(
        select(
            Instance.project_id,
            func.count(),
            dimension(Instance.vcpus, "vcpu"),
            dimension(Instance.ram_mb, "ram"),
            dimension(Instance.root_disk_gb, "root_disk"),
            dimension(Instance.ephemeral_disk_gb, "ephemeral_disk"),
            func.sum(
                case(
                    (
                        (
                            Instance.vcpus.is_(None)
                            | Instance.ram_mb.is_(None)
                            | Instance.root_disk_gb.is_(None)
                            | Instance.ephemeral_disk_gb.is_(None)
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(case((Instance.missing_scans > 0, 1), else_=0)),
        )
        .where(
            Instance.cloud_id == cloud_id,
            Instance.is_missing.is_(False),
            Instance.deleted_at_openstack.is_(None),
            Instance.status.in_(policy.nova.counted_states),
        )
        .group_by(Instance.project_id)
    )
    for pid, count, cpu, ram, root, ephemeral, incomplete, pending in instances:
        summaries[pid].update(
            instance_count=count,
            vcpu_count=cpu,
            ram_gb=Decimal(ram) / Decimal(1024),
            nova_root_disk_gb=root,
            nova_ephemeral_disk_gb=ephemeral,
            incomplete_instances=incomplete,
            pending_missing_instances=pending,
        )
    volumes = db.execute(
        select(
            Volume.project_id,
            func.count(),
            func.coalesce(func.sum(Volume.size_gb), 0),
            func.sum(case((Volume.size_gb.is_(None), 1), else_=0)),
            func.sum(case((Volume.missing_scans > 0, 1), else_=0)),
        )
        .where(
            Volume.cloud_id == cloud_id,
            Volume.is_missing.is_(False),
            Volume.status.in_(policy.cinder.counted_states),
        )
        .group_by(Volume.project_id)
    )
    for pid, count, size, incomplete, pending in volumes:
        summaries[pid].update(
            cinder_volume_count=count,
            cinder_volume_gb=size,
            incomplete_volumes=incomplete,
            pending_missing_volumes=pending,
        )
    return list(summaries.values())


def cloud_summary(rows):
    return {
        "projects": sum(not row["is_missing"] for row in rows),
        **{metric: sum(row[metric] for row in rows) for metric in METRICS},
    }
