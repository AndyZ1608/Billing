from uuid import uuid4

from sqlalchemy import exists, or_, select

from app.core.jobs import CloudLock, JobBusy
from app.core.logging import event
from app.metering.math import quantity, rounded, seconds, utc
from app.models import ChargeRecord, InvoiceChargeLink, RatingRun, UsageRecord, utcnow
from app.pricing.resolver import PricingResolver
from app.pricing.service import audit
from app.rating.money import cost


class BilledChargeConflict(ValueError):
    pass


class RatingEngine:
    batch_size = 200

    def __init__(self, engine, sessions, settings, gate=None):
        self.engine, self.sessions, self.settings = engine, sessions, settings
        self.cloud = settings.openstack_cloud_id
        self.version = settings.rating_calculation_version
        self.usage_version = settings.metering_calculation_version
        self.lock = CloudLock(engine, self.cloud, gate)

    def segments(self, usage, resolver):
        if (
            usage.status != "FINAL"
            or usage.allocated_quantity < 0
            or not usage.allocated_quantity.is_finite()
            or usage.usage_quantity < 0
        ):
            raise ValueError("invalid_usage_quantity")
        if utc(usage.period_start) >= utc(usage.period_end) or usage.duration_seconds != seconds(
            usage.period_start, usage.period_end
        ):
            raise ValueError("invalid_usage_duration")
        # The ledger contract is allocated capacity × elapsed time, with Phase 2 rounding.
        if usage.usage_quantity != rounded(
            quantity(usage.allocated_quantity, usage.period_start, usage.period_end)
        ):
            raise ValueError("inconsistent_usage_quantity")
        points = resolver.boundaries(usage)
        resolved = []
        for start, end in zip(points, points[1:]):
            price = resolver.resolve(usage.project_id, usage.meter_name, start, usage.unit)
            if resolved and resolved[-1][2] == price:
                resolved[-1] = (resolved[-1][0], end, price)
            else:
                resolved.append((start, end, price))
        rows = []
        for start, end, price in resolved:
            unrated = price.get("unrated_reason")
            rows.append(
                dict(
                    cloud_id=usage.cloud_id,
                    project_id=usage.project_id,
                    resource_type=usage.resource_type,
                    resource_id=usage.resource_id,
                    usage_record_id=usage.usage_record_id,
                    meter_name=usage.meter_name,
                    unit=usage.unit,
                    rated_period_start=start,
                    rated_period_end=end,
                    duration_seconds=seconds(start, end),
                    allocated_quantity=usage.allocated_quantity,
                    usage_quantity=usage.usage_quantity,
                    rated_quantity=rounded(quantity(usage.allocated_quantity, start, end)),
                    rating_version=self.version,
                    source_usage_status="FINAL",
                    status="UNRATED" if unrated else "RATED",
                    subtotal=None
                    if unrated
                    else cost(usage.allocated_quantity, start, end, price["unit_price"]),
                    **price,
                )
            )
        return rows

    def persist_usage(self, db, usage, resolver, run, force):
        rows = self.segments(usage, resolver)
        old = list(
            db.scalars(
                select(ChargeRecord).where(
                    ChargeRecord.usage_record_id == usage.usage_record_id, ChargeRecord.status != "SUPERSEDED"
                )
            )
        )
        if old and not force:
            # A normal retry may fill gaps, but must not change any already RATED snapshot.
            if all(r.status == "RATED" for r in old):
                return 0, len(old), 0
            old = [r for r in old if r.status == "UNRATED"]
            retry_rows = []
            for previous in old:
                for values in rows:
                    lower = max(utc(previous.rated_period_start), values["rated_period_start"])
                    upper = min(utc(previous.rated_period_end), values["rated_period_end"])
                    if lower < upper:
                        retry_rows.append(
                            {
                                **values,
                                "rated_period_start": lower,
                                "rated_period_end": upper,
                                "duration_seconds": seconds(lower, upper),
                                "rated_quantity": rounded(quantity(usage.allocated_quantity, lower, upper)),
                                "subtotal": None
                                if values["status"] == "UNRATED"
                                else cost(usage.allocated_quantity, lower, upper, values["unit_price"]),
                            }
                        )
            rows = retry_rows
            if len(old) == len(rows):
                by_start = {utc(r.rated_period_start): r for r in old}
                same = True
                for row in rows:
                    previous = by_start.get(row["rated_period_start"])
                    for key, value in row.items():
                        actual = getattr(previous, key, None) if previous else None
                        if hasattr(value, "tzinfo") and actual is not None:
                            actual = utc(actual)
                            value = utc(value)
                        if actual != value:
                            same = False
                            break
                    if not same:
                        break
                if same:
                    return 0, len(old), sum(r.status == "UNRATED" for r in old)
        if old and db.scalar(
            select(InvoiceChargeLink.id)
            .where(
                InvoiceChargeLink.charge_record_id.in_([r.id for r in old]),
                InvoiceChargeLink.locked_at.is_not(None),
            )
            .limit(1)
        ):
            raise BilledChargeConflict("BILLED_CHARGE_CONFLICT")
        for record in old:
            record.status = "SUPERSEDED"
            record.superseded_at = utcnow()
            record.superseded_by_rating_run_id = run.id
        db.flush()
        for values in rows:
            db.add(ChargeRecord(**values, rating_run_id=run.id))
        db.flush()
        return len(rows), 0, sum(r["status"] == "UNRATED" for r in rows)

    def run(self, start=None, end=None, force=False, actor="scheduler"):
        if (start is None) != (end is None) or start is not None and utc(start) >= utc(end):
            raise ValueError("Provide a positive range with both bounds")
        if force and start is None:
            raise ValueError("Force re-rating requires an explicit bounded range")
        with self.lock.held():
            cutoff = utcnow()
            run_id = uuid4()
            with self.sessions.begin() as db:
                for old in db.scalars(
                    select(RatingRun).where(RatingRun.cloud_id == self.cloud, RatingRun.status == "RUNNING")
                ):
                    old.status, old.finished_at = "FAILED", cutoff
                    old.errors = [{"code": "interrupted_previous_run"}]
                run = RatingRun(
                    id=run_id,
                    cloud_id=self.cloud,
                    period_start=start,
                    period_end=end,
                    rating_version=self.version,
                    usage_calculation_version=self.usage_version,
                    force=force,
                    actor=actor,
                    started_at=cutoff,
                )
                db.add(run)
                db.flush()
                if force:
                    audit(db, self.cloud, "FORCE_RERATING_STARTED", run, actor)
            event("RATING_STARTED", cloud_id=self.cloud, rating_run_id=run_id, force=force)
            try:
                with self.sessions() as db:
                    resolver = PricingResolver(db, self.cloud)
                last = None
                while True:
                    with self.sessions.begin() as db:
                        query = select(UsageRecord).where(
                            UsageRecord.cloud_id == self.cloud,
                            UsageRecord.status == "FINAL",
                            UsageRecord.calculation_version == self.usage_version,
                            UsageRecord.created_at <= cutoff,
                        )
                        if start is not None:
                            query = query.where(
                                UsageRecord.period_start < end, UsageRecord.period_end > start
                            )
                        if last is not None:
                            query = query.where(UsageRecord.usage_record_id > last)
                        if not force:
                            current = select(ChargeRecord.id).where(
                                ChargeRecord.usage_record_id == UsageRecord.usage_record_id,
                                ChargeRecord.status != "SUPERSEDED",
                            )
                            unresolved = select(ChargeRecord.id).where(
                                ChargeRecord.usage_record_id == UsageRecord.usage_record_id,
                                ChargeRecord.status == "UNRATED",
                            )
                            query = query.where(or_(~exists(current), exists(unresolved)))
                        usages = list(
                            db.scalars(query.order_by(UsageRecord.usage_record_id).limit(self.batch_size))
                        )
                        if not usages:
                            break
                        run = db.get(RatingRun, run_id)
                        for usage in usages:
                            run.usage_records_processed += 1
                            try:
                                with db.begin_nested():
                                    created, skipped, unrated = self.persist_usage(
                                        db, usage, resolver, run, force
                                    )
                                run.charge_records_created += created
                                run.charge_records_skipped += skipped
                                run.unrated_records += unrated
                            except Exception as exc:
                                # Do not expose SQL/SDK exception bodies or leave a half-replaced usage.
                                if len(run.errors) < 100:
                                    run.errors = run.errors + [
                                        {
                                            "usage_record_id": str(usage.usage_record_id),
                                            "code": "BILLED_CHARGE_CONFLICT"
                                            if isinstance(exc, BilledChargeConflict)
                                            else "usage_rating_failed",
                                        }
                                    ]
                                run.status = "PARTIAL"
                            last = usage.usage_record_id
                with self.sessions.begin() as db:
                    run = db.get(RatingRun, run_id)
                    run.status = (
                        "PARTIAL"
                        if run.errors or run.unrated_records or run.status == "PARTIAL"
                        else "SUCCESS"
                    )
                    run.finished_at = utcnow()
            except Exception:
                with self.sessions.begin() as db:
                    run = db.get(RatingRun, run_id)
                    run.status, run.finished_at = "FAILED", utcnow()
                    run.errors = run.errors + [{"code": "rating_run_failed"}]
                event("RATING_FAILED", cloud_id=self.cloud, rating_run_id=run_id)
            with self.sessions() as db:
                result = db.get(RatingRun, run_id)
                event(
                    "RATING_COMPLETED",
                    cloud_id=self.cloud,
                    rating_run_id=run_id,
                    status=result.status,
                    charges=result.charge_records_created,
                )
                return result

    def after_metering(self):
        if not self.settings.rating_enabled:
            return
        try:
            self.run()
        except JobBusy:
            event("RATING_SKIPPED", cloud_id=self.cloud, code="cloud_busy")
