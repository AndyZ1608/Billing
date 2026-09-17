from collections import defaultdict
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.metering.math import quantity, rounded, seconds, split_days, utc
from app.metering.policy import MeteringPolicy
from app.metering.registry import BY_NAME, METERS, allocations
from app.metering.storage import billable_windows
from app.models import MeteringPolicyVersion, Project, StatePeriod, UsageRecord, utcnow


def scoped_periods(cloud_id, start=None, end=None, project_id=None, resource_type=None, resource_id=None):
    query = select(StatePeriod).where(StatePeriod.cloud_id == cloud_id)
    if start is not None:
        query = query.where(or_(StatePeriod.valid_to.is_(None), StatePeriod.valid_to > start))
    if end is not None:
        query = query.where(StatePeriod.valid_from < end)
    if project_id:
        query = query.where(StatePeriod.project_id == project_id)
    if resource_type:
        query = query.where(StatePeriod.resource_type == resource_type)
    if resource_id:
        query = query.where(StatePeriod.resource_id == resource_id)
    return query


def usage_view(
    db,
    cloud_id,
    version,
    start,
    end,
    project_id=None,
    resource_type=None,
    resource_id=None,
    meter=None,
    now=None,
):
    """Final record intersections + provisional gaps; never reads current allocations."""
    policy_row = db.get(MeteringPolicyVersion, (cloud_id, version))
    if policy_row is None:
        raise LookupError("Unknown calculation version; run metering to register it")
    policy = MeteringPolicy.model_validate(policy_row.policy)
    cutoff = min(utc(end), utc(now or utcnow()))
    start = utc(start)
    if cutoff <= start:
        return [], [], cutoff
    periods = list(
        db.scalars(
            scoped_periods(cloud_id, start, cutoff, project_id, resource_type, resource_id).order_by(
                StatePeriod.valid_from, StatePeriod.period_id
            )
        )
    )
    ids = [p.period_id for p in periods]
    records = defaultdict(list)
    for offset in range(0, len(ids), 400):
        for record in db.scalars(
            select(UsageRecord).where(
                UsageRecord.source_state_period_id.in_(ids[offset : offset + 400]),
                UsageRecord.calculation_version == version,
                UsageRecord.period_start < cutoff,
                UsageRecord.period_end > start,
            )
        ):
            records[record.source_state_period_id].append(record)
    rows, issues = [], []
    for period in periods:
        capacities, quality = allocations(period, policy)
        stop = min(cutoff, utc(period.valid_to)) if period.valid_to else cutoff
        windows, storage_issues, _ = billable_windows(
            db, period, policy, max(start, utc(period.valid_from)), stop
        )
        quality += storage_issues
        if quality:
            issues.append(
                {
                    "resource_type": period.resource_type,
                    "resource_id": period.resource_id,
                    "source_state_period_id": period.period_id,
                    "codes": quality,
                }
            )
        common = {
            "cloud_id": cloud_id,
            "project_id": period.project_id,
            "resource_type": period.resource_type,
            "resource_id": period.resource_id,
            "resource_name": period.resource_name,
            "source_state_period_id": period.period_id,
            "source_observation_id": period.source_observation_id,
            "history_confidence": period.history_confidence,
            "state": period.state,
            "calculation_version": version,
        }
        final = records[period.period_id]
        if final:
            segments = [
                (
                    r.meter_name,
                    r.unit,
                    r.allocated_quantity,
                    max(start, utc(r.period_start)),
                    min(cutoff, utc(r.period_end)),
                    "FINAL",
                    r.usage_record_id,
                )
                for r in final
            ]
        else:
            stop = min(cutoff, utc(period.valid_to)) if period.valid_to else cutoff
            segments = [
                (m.name, m.unit, allocated, a, b, "PROVISIONAL", None)
                for a, b in windows
                for m, allocated in capacities
            ]
        for name, unit, allocated, lower, upper, status, record_id in segments:
            if lower >= upper or meter and name != meter:
                continue
            rows.append(
                {
                    **common,
                    "usage_record_id": record_id,
                    "meter_name": name,
                    "unit": unit,
                    "allocated_quantity": allocated,
                    "period_start": lower,
                    "period_end": upper,
                    "duration_seconds": seconds(lower, upper),
                    "usage_quantity": quantity(allocated, lower, upper),
                    "status": status,
                    "provisional_reason": None
                    if status == "FINAL"
                    else "OPEN_PERIOD"
                    if period.valid_to is None
                    else "AWAITING_METERING",
                }
            )
    return rows, issues, cutoff


def totals(rows):
    result = {meter.summary_key: Decimal(0) for meter in METERS}
    final, provisional = dict(result), dict(result)
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            key = BY_NAME[row["meter_name"]].summary_key
            result[key] += row["usage_quantity"]
            target = final if row["status"] == "FINAL" else provisional
            target[key] += row["usage_quantity"]
    return {
        **{key: rounded(value) for key, value in result.items()},
        "final": {key: rounded(value) for key, value in final.items()},
        "provisional": {key: rounded(value) for key, value in provisional.items()},
    }


def group_projects(db, cloud_id, rows):
    names = dict(
        db.execute(select(Project.project_id, Project.project_name).where(Project.cloud_id == cloud_id)).all()
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["project_id"]].append(row)
    return [
        {
            "project_id": project_id,
            "project_name": names.get(project_id, "Historical project"),
            **totals(items),
        }
        for project_id, items in sorted(grouped.items(), key=lambda item: str(item[0]))
    ]


def group_resources(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["project_id"], row["resource_type"], row["resource_id"])].append(row)
    result = [
        {
            "project_id": key[0],
            "resource_type": key[1],
            "resource_id": key[2],
            "resource_name": items[-1]["resource_name"],
            **totals(items),
        }
        for key, items in grouped.items()
    ]
    result.sort(key=lambda row: (row["vcpu_hours"], row["volume_gib_hours"]), reverse=True)
    return result


def daily_totals(rows, timezone):
    zone = ZoneInfo(timezone)
    grouped = defaultdict(list)
    for row in rows:
        for start, end in split_days(row["period_start"], row["period_end"], timezone):
            grouped[start.astimezone(zone).date().isoformat()].append(
                {**row, "usage_quantity": quantity(row["allocated_quantity"], start, end)}
            )
    return [{"date": day, **totals(items)} for day, items in sorted(grouped.items())]
