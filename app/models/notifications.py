from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, json_type, utcnow


class ProcessedNotification(Base):
    __tablename__ = "processed_notifications"
    cloud_id: Mapped[UUID] = mapped_column(ForeignKey("clouds.cloud_id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    instance_id: Mapped[UUID | None] = mapped_column(Uuid)
    project_id: Mapped[UUID | None] = mapped_column(Uuid)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32))
    issue_code: Mapped[str | None] = mapped_column(String(64))
    normalized_event: Mapped[dict] = mapped_column(json_type)
    __table_args__ = (
        Index("ix_notifications_resource_time", "cloud_id", "instance_id", "event_timestamp"),
        Index("ix_notifications_quality", "cloud_id", "status", "received_at"),
    )
