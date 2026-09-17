import hashlib
import threading
from time import monotonic
from uuid import uuid4

from sqlalchemy import or_, select, text

from app.core.logging import event
from app.metering.math import quantity, rounded, seconds, split_days, utc
from app.metering.policy import load_metering_policy
from app.metering.registry import allocations
from app.metering.storage import billable_windows
from app.models import MeteringPolicyVersion, MeteringRun, StatePeriod, UsageRecord, utcnow


class MeteringBusy(Exception):
    pass


class PolicyVersionConflict(ValueError):
    pass


class MeteringEngine:
    """Append-only closed-period materialization, serialized with lifecycle synchronization."""

    def __init__(self, engine, sessions, settings, gate=None):
        self.engine, self.sessions, self.settings = engine, sessions, settings
        self.gate = gate if gate is not None else threading.Lock()
        self.policy = load_metering_policy(settings.metering_policy_path)
        self.cloud_id, self.version = settings.openstack_cloud_id, settings.metering_calculation_version
        digest = hashlib.sha256(str(self.cloud_id).encode()).digest()[:8]
        self.lock_key = int.from_bytes(digest, "big", signed=True)
        self.on_completed = None

    def register_policy(self, db):
        registered = db.get(MeteringPolicyVersion, (self.cloud_id, self.version))
        if registered is None:
            registered = MeteringPolicyVersion(
                cloud_id=self.cloud_id,
                calculation_version=self.version,
                policy_hash=self.policy.fingerprint,
                policy=self.policy.snapshot(),
            )
            db.add(registered)
            db.flush()
        elif registered.policy_hash != self.policy.fingerprint:
            raise PolicyVersionConflict("Metering policy changed: select a new METERING_CALCULATION_VERSION")
        return registered

    def _acquire(self):
        if not self.gate.acquire(blocking=False):
            raise MeteringBusy()
        connection = None
        try:
            if self.engine.dialect.name == "postgresql":
                connection = self.engine.connect()
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self.lock_key}
                )
                connection.commit()
                if not acquired:
                    raise MeteringBusy()
            return connection
        except Exception:
            if connection:
                connection.close()
            self.gate.release()
            raise

    def _release(self, connection):
        try:
            if connection:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.lock_key})
                    connection.commit()
                except Exception:
                    connection.invalidate()
                finally:
                    connection.close()
        finally:
            self.gate.release()

    def run(self, start=None, end=None, force=False, now=None):
        if (start is None) != (end is None) or start and utc(start) >= utc(end):
            raise ValueError("Provide both start and end, with start before end")
        if force and start is None:
            raise ValueError("Explicit re-metering requires a bounded range")
        cutoff, clock = utc(now or utcnow()), monotonic()
        connection = self._acquire()
        run_id = uuid4()
        created = False
        try:
            with self.sessions.begin() as db:
                version = self.register_policy(db)
                for old in db.scalars(
                    select(MeteringRun).where(
                        MeteringRun.cloud_id == self.cloud_id, MeteringRun.status == "RUNNING"
                    )
                ):
                    old.status, old.finished_at = "FAILED", cutoff
                    old.errors = [{"code": "interrupted_previous_run"}]
                db.add(
                    MeteringRun(
                        metering_run_id=run_id,
                        cloud_id=self.cloud_id,
                        calculation_version=self.version,
                        started_at=cutoff,
                        requested_start=start,
                        requested_end=end,
                        watermark_from=version.watermark,
                    )
                )
            created = True
            event(
                "METERING_STARTED",
                cloud_id=self.cloud_id,
                metering_run_id=run_id,
                calculation_version=self.version,
            )
            with self.sessions.begin() as db:
                version = self.register_policy(db)
                query = select(StatePeriod).where(
                    StatePeriod.cloud_id == self.cloud_id,
                    StatePeriod.valid_to.is_not(None),
                    StatePeriod.closed_at <= cutoff,
                )
                if start:
                    query = query.where(StatePeriod.valid_from < end, StatePeriod.valid_to > start)
                elif version.watermark:
                    # Inclusive boundary safely handles multiple closures at the same clock tick.
                    condition = StatePeriod.closed_at >= version.watermark
                    if self.policy.volume.active_attachment_only:
                        # Retry closed volumes whose VM history was not yet stable.
                        pending = (
                            ~select(UsageRecord.usage_record_id)
                            .where(
                                UsageRecord.source_state_period_id == StatePeriod.period_id,
                                UsageRecord.calculation_version == self.version,
                            )
                            .exists()
                        )
                        condition = or_(condition, (StatePeriod.resource_type == "VOLUME") & pending)
                    query = query.where(condition)
                periods = list(db.scalars(query.order_by(StatePeriod.closed_at, StatePeriod.period_id)))
                ids = [p.period_id for p in periods]
                existing = set()
                # Chunk large IN lists so the correctness-first POC also works with SQLite limits.
                for offset in range(0, len(ids), 400):
                    for record in db.scalars(
                        select(UsageRecord).where(
                            UsageRecord.source_state_period_id.in_(ids[offset : offset + 400]),
                            UsageRecord.calculation_version == self.version,
                        )
                    ):
                        existing.add(
                            (
                                record.source_state_period_id,
                                record.meter_name,
                                utc(record.period_start),
                                utc(record.period_end),
                            )
                        )
                count, reused, errors = 0, 0, []
                for period in periods:
                    capacity, issues = allocations(period, self.policy)
                    windows, storage_issues, stable = billable_windows(
                        db, period, self.policy, period.valid_from, period.valid_to
                    )
                    issues += storage_issues
                    if not stable:
                        issues.append("awaiting_stable_vm_history")
                        capacity = []
                    if issues:
                        errors.append(
                            {
                                "source_state_period_id": str(period.period_id),
                                "resource_id": str(period.resource_id),
                                "codes": issues,
                            }
                        )
                        event(
                            "METERING_QUALITY_WARNING",
                            cloud_id=self.cloud_id,
                            project_id=period.project_id,
                            resource_type=period.resource_type,
                            resource_id=period.resource_id,
                            metering_run_id=run_id,
                            codes=issues,
                        )
                    day_segments = [segment for a, b in windows for segment in split_days(a, b)]
                    for segment_start, segment_end in day_segments:
                        for meter, allocated in capacity:
                            key = (period.period_id, meter.name, segment_start, segment_end)
                            if key in existing:
                                reused += 1
                                continue
                            db.add(
                                UsageRecord(
                                    cloud_id=self.cloud_id,
                                    project_id=period.project_id,
                                    resource_type=period.resource_type,
                                    resource_id=period.resource_id,
                                    meter_name=meter.name,
                                    unit=meter.unit,
                                    period_start=segment_start,
                                    period_end=segment_end,
                                    duration_seconds=seconds(segment_start, segment_end),
                                    allocated_quantity=allocated,
                                    usage_quantity=rounded(quantity(allocated, segment_start, segment_end)),
                                    source_state_period_id=period.period_id,
                                    calculation_version=self.version,
                                    metering_run_id=run_id,
                                )
                            )
                            existing.add(key)
                            count += 1
                run = db.get(MeteringRun, run_id)
                run.status = "PARTIAL" if errors else "SUCCESS"
                run.finished_at = utcnow()
                run.state_periods_processed, run.usage_records_created, run.usage_records_reused = (
                    len(periods),
                    count,
                    reused,
                )
                run.errors = errors
                if start is None:
                    version.watermark = max(cutoff, utc(version.watermark)) if version.watermark else cutoff
                run.watermark_to = version.watermark
            event(
                "METERING_COMPLETED",
                cloud_id=self.cloud_id,
                metering_run_id=run_id,
                state_periods_processed=len(periods),
                usage_records_created=count,
                usage_records_reused=reused,
                duration_seconds=round(monotonic() - clock, 3),
            )
        except PolicyVersionConflict:
            raise
        except Exception:
            event(
                "METERING_FAILED", cloud_id=self.cloud_id, metering_run_id=run_id, code="calculation_failed"
            )
            if created:
                with self.sessions.begin() as db:
                    run = db.get(MeteringRun, run_id)
                    run.status, run.finished_at = "FAILED", utcnow()
                    run.errors = [{"code": "calculation_failed"}]
            else:
                raise
        finally:
            self._release(connection)
        if self.on_completed:
            self.on_completed()
        with self.sessions() as db:
            return db.get(MeteringRun, run_id)

    def after_sync(self):
        try:
            self.run()
        except MeteringBusy:
            # Another accepted operation owns the cloud; its completion/next poll retries.
            event("METERING_SKIPPED", cloud_id=self.cloud_id, code="cloud_busy")
