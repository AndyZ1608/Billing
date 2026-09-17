from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from zoneinfo import ZoneInfo

QUANTUM = Decimal("0.000000000001")


def utc(value):
    # Only DB reads from SQLite tests may be naive. Public inputs require offsets.
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def seconds(start, end):
    delta = utc(end) - utc(start)
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)


def quantity(capacity, start, end):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(capacity) * seconds(start, end) / Decimal(3600)


def rounded(value):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(value).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def split_days(start, end, timezone="UTC"):
    start, end, zone = utc(start), utc(end), ZoneInfo(timezone)
    while start < end:
        tomorrow = start.astimezone(zone).date() + timedelta(days=1)
        boundary = datetime.combine(tomorrow, time.min, tzinfo=zone).astimezone(UTC)
        stop = min(end, boundary)
        yield start, stop
        start = stop
