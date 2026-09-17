"""Initial cloud-scoped inventory, synchronization and observation schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def stamp(name, nullable=True):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def cloud_column(primary=False):
    return sa.Column(
        "cloud_id", sa.Uuid(), sa.ForeignKey("clouds.cloud_id"), primary_key=primary, nullable=False
    )


def seen():
    return [
        stamp("first_seen_at", False),
        stamp("last_seen_at", False),
        stamp("created_at", False),
        stamp("updated_at", False),
        stamp("missing_since"),
        sa.Column("missing_scans", sa.Integer(), nullable=False),
        sa.Column("is_missing", sa.Boolean(), nullable=False),
    ]


def project_fk():
    return sa.ForeignKeyConstraint(["cloud_id", "project_id"], ["projects.cloud_id", "projects.project_id"])


def upgrade():
    op.create_table(
        "clouds",
        sa.Column("cloud_id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("region", sa.String(255), nullable=False),
        sa.Column("connection_status", sa.String(20), nullable=False),
        stamp("last_attempt_at"),
        stamp("last_successful_sync"),
        stamp("last_failed_sync"),
        sa.Column("service_status", JSON, nullable=False),
    )
    op.create_table(
        "projects",
        cloud_column(True),
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column("domain_id", sa.String(255)),
        sa.Column("enabled", sa.Boolean()),
        sa.Column("is_placeholder", sa.Boolean(), nullable=False),
        *seen(),
    )
    op.create_table(
        "instances",
        cloud_column(True),
        sa.Column("instance_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("instance_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        stamp("created_at_openstack"),
        stamp("updated_at_openstack"),
        stamp("deleted_at_openstack"),
        sa.Column("flavor_id", sa.String(255)),
        sa.Column("flavor_name", sa.String(255)),
        sa.Column("vcpus", sa.Integer()),
        sa.Column("ram_mb", sa.Integer()),
        sa.Column("root_disk_gb", sa.Integer()),
        sa.Column("ephemeral_disk_gb", sa.Integer()),
        sa.Column("boot_source", sa.String(20), nullable=False),
        sa.Column("host", sa.String(255)),
        sa.Column("availability_zone", sa.String(255)),
        sa.Column("quality_issues", JSON, nullable=False),
        sa.Column("raw_payload", JSON),
        *seen(),
        project_fk(),
    )
    op.create_index("ix_instances_cloud_project", "instances", ["cloud_id", "project_id"])
    op.create_table(
        "volumes",
        cloud_column(True),
        sa.Column("volume_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("volume_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("volume_type", sa.String(255)),
        sa.Column("size_gb", sa.Integer()),
        sa.Column("bootable", sa.Boolean()),
        stamp("created_at_openstack"),
        stamp("updated_at_openstack"),
        sa.Column("attachments", JSON, nullable=False),
        sa.Column("quality_issues", JSON, nullable=False),
        sa.Column("raw_payload", JSON),
        *seen(),
        project_fk(),
    )
    op.create_index("ix_volumes_cloud_project", "volumes", ["cloud_id", "project_id"])
    op.create_table(
        "sync_runs",
        sa.Column("sync_run_id", sa.Uuid(), primary_key=True),
        cloud_column(),
        stamp("started_at", False),
        stamp("finished_at"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("projects_found", sa.Integer(), nullable=False),
        sa.Column("instances_found", sa.Integer(), nullable=False),
        sa.Column("volumes_found", sa.Integer(), nullable=False),
        sa.Column("errors", JSON, nullable=False),
        sa.Column("services", JSON, nullable=False),
    )
    op.create_index("ix_sync_runs_cloud_id", "sync_runs", ["cloud_id"])
    op.create_table(
        "resource_observations",
        sa.Column("observation_id", sa.Uuid(), primary_key=True),
        cloud_column(),
        sa.Column("sync_run_id", sa.Uuid(), sa.ForeignKey("sync_runs.sync_run_id"), nullable=False),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        stamp("observed_at", False),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("normalized_payload", JSON, nullable=False),
    )
    op.create_index(
        "ix_observations_resource_time",
        "resource_observations",
        ["cloud_id", "resource_type", "resource_id", "observed_at"],
    )
    op.create_index("ix_observations_run", "resource_observations", ["sync_run_id"])


def downgrade():
    for table in ("resource_observations", "sync_runs", "volumes", "instances", "projects", "clouds"):
        op.drop_table(table)
