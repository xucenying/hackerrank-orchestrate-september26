"""Output formatting conventions, matched to dataset/sample_requests.csv.

* ``payment_plan`` and ``reduce_to`` amounts: two decimals when fractional (``620.40``), none
  when whole (``25256``).
* ``amount_safe_to_pay``: quantised to cents, trailing zeros dropped (``603.3``, ``17229139.2``).
* Dates: ISO ``YYYY-MM-DD``. Human dates for explanations: ``8 August 2025``.
* Money in prose: ``ZAR 25,256`` / ``EUR 620.40``.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_plan_amount(x: Decimal) -> str:
    v = q2(x)
    if v == v.to_integral_value():
        return str(int(v))
    return f"{v:.2f}"


def fmt_safe_amount(x: Decimal) -> str:
    v = q2(x).normalize()
    if v == v.to_integral_value():
        return str(int(v))
    return format(v, "f")


def fmt_money_prose(x: Decimal, currency: str) -> str:
    v = q2(x)
    if v == v.to_integral_value():
        return f"{currency} {int(v):,}"
    return f"{currency} {v:,.2f}"


def fmt_date_prose(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def fmt_plan(payments) -> str:
    """``payments``: iterable of objects with ``on`` and ``amount``."""
    items = sorted(payments, key=lambda p: p.on)
    return "|".join(f"{p.on.isoformat()}:{fmt_plan_amount(p.amount)}" for p in items) if items else "none"
