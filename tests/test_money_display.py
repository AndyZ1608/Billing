import json
from decimal import Decimal

import pytest

from app.api.pricing import response
from app.rating.money import display_money, money


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2020858.40310001", "2020858"),
        ("2020858.60310001", "2020859"),
        ("2.5", "3"),
        ("-2.5", "-3"),
        ("999.99999999", "1000"),
        ("0.499999999999", "0"),
        ("-0.0001", "0"),
        ("9007199254740993.5", "9007199254740994"),
    ],
)
def test_vnd_presentation_is_whole_half_up_without_mutating_raw(raw, expected):
    original = Decimal(raw)
    displayed = display_money(original, "VND")
    assert format(displayed, "f") == expected
    assert original == Decimal(raw)
    payload = json.loads(response({"subtotal": original, "display_total": displayed}).body)
    assert payload["subtotal"] == raw
    assert payload["display_total"] == expected
    assert "." not in payload["display_total"]


def test_internal_quantization_and_other_currencies_are_unchanged():
    assert money(Decimal("2.50000001")) == Decimal("2.50000001")
    assert display_money(Decimal("1.005"), "USD") == Decimal("1.00")
    with pytest.raises(TypeError):
        display_money(2.5, "VND")
