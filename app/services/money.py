"""Decimal-safe money maths shared by invoices and quotations.

All stored amounts are integer minor units (cents). Quantities and tax/discount
rates are Decimals; rounding happens in one place, half-up, never with floats.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class MoneyError(ValueError):
    pass


@dataclass(frozen=True)
class LineInput:
    quantity: Decimal
    unit_price_cents: int
    tax_rate: Decimal | None = None


@dataclass(frozen=True)
class Totals:
    line_totals: list[int]
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int


def to_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_totals(
    lines: list[LineInput],
    *,
    discount_percent: Decimal | None = None,
    discount_cents: int | None = None,
) -> Totals:
    """Line totals, discount, tax and grand total.

    Discount is either a percentage of the subtotal or a fixed amount (not both).
    Tax is computed per line using that line's rate, then scaled by the share of
    the subtotal that remains after the discount, and rounded once.
    """
    if not lines:
        raise MoneyError("At least one line item is required")
    if discount_percent is not None and discount_cents:
        raise MoneyError("Provide either a discount percentage or a fixed discount, not both")

    line_totals: list[int] = []
    for line in lines:
        if line.quantity <= 0:
            raise MoneyError("Quantity must be greater than zero")
        if line.unit_price_cents < 0:
            raise MoneyError("Unit price cannot be negative")
        if line.tax_rate is not None and not (Decimal(0) <= line.tax_rate <= Decimal(100)):
            raise MoneyError("Tax rate must be between 0 and 100")
        line_totals.append(to_cents(line.quantity * line.unit_price_cents))

    subtotal = sum(line_totals)

    if discount_percent is not None:
        if not (Decimal(0) <= discount_percent <= Decimal(100)):
            raise MoneyError("Discount percentage must be between 0 and 100")
        discount = to_cents(Decimal(subtotal) * discount_percent / Decimal(100))
    else:
        discount = discount_cents or 0
        if discount < 0:
            raise MoneyError("Discount cannot be negative")
    if discount > subtotal:
        raise MoneyError("Discount cannot exceed the subtotal")

    raw_tax = sum(
        (Decimal(total) * (line.tax_rate or Decimal(0)) / Decimal(100) for total, line in zip(line_totals, lines)),
        Decimal(0),
    )
    if subtotal > 0:
        raw_tax = raw_tax * Decimal(subtotal - discount) / Decimal(subtotal)
    tax = to_cents(raw_tax)

    return Totals(
        line_totals=line_totals,
        subtotal_cents=subtotal,
        discount_cents=discount,
        tax_cents=tax,
        total_cents=subtotal - discount + tax,
    )
