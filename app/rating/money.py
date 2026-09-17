from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

from app.metering.math import quantity

MONEY_QUANTUM = Decimal("0.00000001")
CURRENCIES = {"VND": Decimal("1"), "USD": Decimal("0.01")}


def money(value):
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def cost(capacity, start, end, price):
    with localcontext() as ctx:
        ctx.prec = 50
        return money(quantity(capacity, start, end) * price)


def display_money(value, currency):
    with localcontext() as ctx:
        ctx.prec = 50
        if currency == "VND":
            # Presentation only: avoid an intermediate quantize that could double-round.
            if isinstance(value, float):
                raise TypeError("VND presentation requires Decimal or a decimal string")
            rounded = Decimal(value).quantize(CURRENCIES[currency], rounding=ROUND_HALF_UP)
            return rounded if rounded else Decimal(0)
        return money(value).quantize(CURRENCIES[currency], rounding=ROUND_HALF_EVEN)
