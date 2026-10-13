from datetime import date

import pytest

from app.services.schedules import add_months, occurrence_date

# ------------------------------------------------------------------ recurrence maths (pure)


def test_month_end_does_not_drift():
    first = date(2027, 1, 31)
    got = [occurrence_date(first, "monthly", n) for n in range(6)]
    assert got == [
        date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31),
        date(2027, 4, 30), date(2027, 5, 31), date(2027, 6, 30),
    ]


def test_leap_year_handling():
    assert occurrence_date(date(2028, 1, 31), "monthly", 1) == date(2028, 2, 29)
    assert occurrence_date(date(2028, 2, 29), "annually", 1) == date(2029, 2, 28)
    assert occurrence_date(date(2028, 2, 29), "annually", 4) == date(2032, 2, 29)


def test_quarterly_weekly_and_year_rollover():
    assert [occurrence_date(date(2026, 11, 30), "quarterly", n) for n in range(3)] == [
        date(2026, 11, 30), date(2027, 2, 28), date(2027, 5, 30),
    ]
    assert occurrence_date(date(2026, 12, 25), "weekly", 2) == date(2027, 1, 8)
    assert add_months(date(2026, 12, 15), 2) == date(2027, 2, 15)
    assert occurrence_date(date(2026, 5, 5), "once", 0) == date(2026, 5, 5)
    with pytest.raises(ValueError):
        occurrence_date(date(2026, 5, 5), "once", 1)



