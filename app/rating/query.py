from collections import defaultdict
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.metering.math import quantity, rounded, split_days, utc
from app.models import ChargeRecord, Project, UsageRecord
from app.rating.money import cost, display_money, money


def charge_query(
    cloud,
    usage_version,
    start,
    end,
    project_id=None,
    resource_id=None,
    meter=None,
    product=None,
    status=None,
    currency=None,
    **unused,
):
    query = (
        select(ChargeRecord)
        .join(UsageRecord, UsageRecord.usage_record_id == ChargeRecord.usage_record_id)
        .where(
            ChargeRecord.cloud_id == cloud,
            UsageRecord.calculation_version == usage_version,
            ChargeRecord.rated_period_start < end,
            ChargeRecord.rated_period_end > start,
        )
    )
    query = (
        query.where(ChargeRecord.status == status)
        if status
        else query.where(ChargeRecord.status != "SUPERSEDED")
    )
    for field, value in (
        (ChargeRecord.project_id, project_id),
        (ChargeRecord.resource_id, resource_id),
        (ChargeRecord.meter_name, meter),
        (ChargeRecord.product_id, product),
        (ChargeRecord.currency, currency),
    ):
        if value is not None:
            query = query.where(field == value)
    return query


def clipped(row, start, end):
    lower, upper = max(utc(row.rated_period_start), utc(start)), min(utc(row.rated_period_end), utc(end))
    amount = None
    if row.subtotal is not None:
        amount = (
            row.subtotal
            if lower == utc(row.rated_period_start) and upper == utc(row.rated_period_end)
            else cost(row.allocated_quantity, lower, upper, row.unit_price)
        )
    return lower, upper, amount


def summarize(db, filters):
    buckets = {}
    names = dict(
        db.execute(
            select(Project.project_id, Project.project_name).where(Project.cloud_id == filters["cloud"])
        ).all()
    )
    with localcontext() as ctx:
        ctx.prec = 50
        for record in db.scalars(
            charge_query(**filters).order_by(ChargeRecord.id).execution_options(yield_per=200)
        ):
            lower, upper, amount = clipped(record, filters["start"], filters["end"])
            currency = record.currency or "UNSPECIFIED"
            if currency not in buckets:
                buckets[currency] = {
                    "currency": currency,
                    "total_charge": Decimal(0),
                    "services": defaultdict(Decimal),
                    "projects": {},
                    "resources": {},
                    "meters": {},
                    "products": {},
                    "daily": {},
                    "monthly": {},
                    "unrated": set(),
                    "rated_projects": set(),
                }
            bucket = buckets[currency]
            if record.status == "UNRATED":
                bucket["unrated"].add(record.usage_record_id)
            if amount is not None:
                bucket["total_charge"] += amount
                bucket["services"][record.service_category or "unknown"] += amount
                bucket["rated_projects"].add(record.project_id)
            keys = (
                (
                    "projects",
                    str(record.project_id),
                    {
                        "project_id": record.project_id,
                        "project_name": names.get(record.project_id, "Historical project"),
                    },
                ),
                (
                    "resources",
                    str(record.project_id) + ":" + record.resource_type + ":" + str(record.resource_id),
                    {
                        "resource_type": record.resource_type,
                        "resource_id": record.resource_id,
                        "project_id": record.project_id,
                    },
                ),
                ("meters", record.meter_name, {"meter_name": record.meter_name, "unit": record.unit}),
                (
                    "products",
                    str(record.product_id),
                    {"product_id": record.product_id, "product_code": record.product_code},
                ),
            )
            for kind, key, identity in keys:
                item = bucket[kind].setdefault(
                    key,
                    {
                        **identity,
                        "currency": currency,
                        "total_charge": Decimal(0),
                        "rated_quantity": Decimal(0),
                        "unrated_segments": 0,
                        "unit_prices": set(),
                    },
                )
                if amount is not None:
                    item["total_charge"] += amount
                    if kind in ("meters", "products"):
                        item["rated_quantity"] += quantity(record.allocated_quantity, lower, upper)
                        item["unit_prices"].add(record.unit_price)
                else:
                    item["unrated_segments"] += 1
            for a, b in split_days(lower, upper, filters["timezone"]):
                date = a.astimezone(ZoneInfo(filters["timezone"])).date().isoformat()
                for field, key in (("daily", date), ("monthly", date[:7])):
                    item = bucket[field].setdefault(
                        key,
                        {
                            "period": key,
                            "currency": currency,
                            "total_charge": Decimal(0),
                            "services": defaultdict(Decimal),
                            "unrated_segments": 0,
                        },
                    )
                    if amount is not None:
                        value = (
                            amount
                            if a == lower and b == upper
                            else cost(record.allocated_quantity, a, b, record.unit_price)
                        )
                        item["total_charge"] += value
                        item["services"][record.service_category or "unknown"] += value
                    else:
                        item["unrated_segments"] += 1
        result = []
        for bucket in buckets.values():
            bucket["unrated_usage_records"] = len(bucket.pop("unrated"))
            bucket["projects_with_charges"] = len(bucket.pop("rated_projects"))
            bucket["total_charge"] = money(bucket["total_charge"])
            bucket["display_total"] = (
                display_money(bucket["total_charge"], bucket["currency"])
                if bucket["currency"] != "UNSPECIFIED"
                else None
            )
            for field in ("projects", "resources", "meters", "products"):
                items = list(bucket[field].values())
                for item in items:
                    if field in ("meters", "products"):
                        item["rated_quantity"] = rounded(item["rated_quantity"])
                        item["unit_prices"] = sorted(item["unit_prices"])
                    else:
                        item.pop("rated_quantity")
                        item.pop("unit_prices")
                bucket[field] = sorted(items, key=lambda i: (-i["total_charge"], str(i)))
            for field in ("daily", "monthly"):
                bucket[field] = [bucket[field][k] for k in sorted(bucket[field])]
            result.append(bucket)
    return sorted(result, key=lambda b: b["currency"])
