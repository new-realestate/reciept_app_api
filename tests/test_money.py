from decimal import Decimal

import pytest

from app.services.money import LineInput, MoneyError, compute_totals

# ------------------------------------------------------------------ money maths


def test_totals_use_decimal_rounding_half_up():
    totals = compute_totals(
        [LineInput(Decimal("3"), 3333, Decimal("18")), LineInput(Decimal("1.5"), 1001, None)],
        discount_percent=Decimal("10"),
    )
    # lines: 9999 + 1502 (1501.5 rounds half up)
    assert totals.line_totals == [9999, 1502]
    assert totals.subtotal_cents == 11501
    assert totals.discount_cents == 1150  # 1150.1
    # tax: 9999*0.18 = 1799.82, scaled by (11501-1150)/11501
    assert totals.tax_cents == 1620
    assert totals.total_cents == 11501 - 1150 + 1620


def test_totals_reject_bad_input():
    with pytest.raises(MoneyError):
        compute_totals([LineInput(Decimal("0"), 100)])
    with pytest.raises(MoneyError):
        compute_totals([LineInput(Decimal("1"), 100)], discount_cents=101)
    with pytest.raises(MoneyError):
        compute_totals([LineInput(Decimal("1"), 100)], discount_percent=Decimal("5"), discount_cents=1)
    with pytest.raises(MoneyError):
        compute_totals([])



