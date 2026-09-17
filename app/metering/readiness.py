"""Metering readiness only: expose unresolved source coverage without computing money."""

from collections import defaultdict

from sqlalchemy import select

from app.metering.math import utc
from app.metering.policy import MeteringPolicy
from app.metering.query import scoped_periods
from app.metering.registry import allocations
from app.metering.storage import billable_windows
from app.models import MeteringPolicyVersion, UsageRecord


def billing_readiness(db, cloud, version, start, end, project=None):
    policy_row = db.get(MeteringPolicyVersion, (cloud, version))
    if policy_row is None:
        return dict(provisional_periods=0, quality_periods=1)
    policy = MeteringPolicy.model_validate(policy_row.policy)
    provisional = quality = 0
    periods = db.scalars(scoped_periods(cloud, start, end, project).execution_options(yield_per=200))
    for batch in periods.partitions(200):
        coverage = defaultdict(list)
        for record in db.scalars(
            select(UsageRecord).where(
                UsageRecord.source_state_period_id.in_([p.period_id for p in batch]),
                UsageRecord.calculation_version == version,
                UsageRecord.period_start < end,
                UsageRecord.period_end > start,
            )
        ):
            coverage[(record.source_state_period_id, record.meter_name)].append(record)
        for period in batch:
            meters, issues = allocations(period, policy)
            quality += bool(issues)
            lower = max(utc(start), utc(period.valid_from))
            upper = min(utc(end), utc(period.valid_to)) if period.valid_to else utc(end)
            windows, storage_issues, _ = billable_windows(db, period, policy, lower, upper)
            quality += bool(storage_issues)
            incomplete = False
            for a, b in windows:
                for meter, _ in meters:
                    cursor = a
                    for record in sorted(
                        coverage[(period.period_id, meter.name)], key=lambda r: utc(r.period_start)
                    ):
                        if utc(record.period_end) <= cursor:
                            continue
                        if utc(record.period_start) > cursor:
                            break
                        cursor = max(cursor, utc(record.period_end))
                    incomplete |= cursor < b
            provisional += incomplete
    return dict(provisional_periods=provisional, quality_periods=quality)
