"""Rating completeness contract used by billing; no usage is calculated here."""

from itertools import groupby

from sqlalchemy import and_, select

from app.metering.math import utc
from app.models import ChargeRecord, UsageRecord


def incomplete_usage(db, cloud, version, start, end, project=None):
    query = (
        select(
            UsageRecord.usage_record_id,
            UsageRecord.period_start,
            UsageRecord.period_end,
            ChargeRecord.rated_period_start,
            ChargeRecord.rated_period_end,
        )
        .outerjoin(
            ChargeRecord,
            and_(
                ChargeRecord.usage_record_id == UsageRecord.usage_record_id,
                ChargeRecord.status == "RATED",
                ChargeRecord.rated_period_start < end,
                ChargeRecord.rated_period_end > start,
            ),
        )
        .where(
            UsageRecord.cloud_id == cloud,
            UsageRecord.calculation_version == version,
            UsageRecord.period_start < end,
            UsageRecord.period_end > start,
        )
    )
    if project is not None:
        query = query.where(UsageRecord.project_id == project)
    query = query.order_by(UsageRecord.usage_record_id, ChargeRecord.rated_period_start)
    count = 0
    # A single streamed outer join detects missing rows and partial price gaps without N+1 queries.
    for _, rows in groupby(db.execute(query.execution_options(yield_per=200)), key=lambda r: r[0]):
        cursor = stop = None
        gap = False
        for _, lower, upper, a, b in rows:
            if cursor is None:
                cursor, stop = max(utc(lower), utc(start)), min(utc(upper), utc(end))
            if a is None or utc(a) > cursor:
                gap = True
            if not gap:
                cursor = max(cursor, utc(b))
        if gap or cursor < stop:
            count += 1
    return count
