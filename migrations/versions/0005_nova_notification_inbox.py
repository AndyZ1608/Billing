"""Add a durable notification inbox without replacing polling observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("resource_observations") as batch:
        batch.alter_column("sync_run_id", existing_type=sa.Uuid(), nullable=True)
    op.create_table(
        "processed_notifications",
        sa.Column("cloud_id", sa.Uuid(), sa.ForeignKey("clouds.cloud_id"), primary_key=True),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.Uuid()),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("event_timestamp", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("issue_code", sa.String(64)),
        sa.Column(
            "normalized_event", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )
    op.create_index(
        "ix_notifications_resource_time",
        "processed_notifications",
        ["cloud_id", "instance_id", "event_timestamp"],
    )
    op.create_index(
        "ix_notifications_quality", "processed_notifications", ["cloud_id", "status", "received_at"]
    )


def downgrade():
    # A populated notification ledger must not be destroyed through normal downgrade.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM processed_notifications")):
        raise RuntimeError("Notification audit exists; downgrade requires an explicit preservation plan")
    op.drop_table("processed_notifications")
    with op.batch_alter_table("resource_observations") as batch:
        batch.alter_column("sync_run_id", existing_type=sa.Uuid(), nullable=False)
