"""Fixed, dated exchange-rate conversion.

Spec (problem_statement.md / AGENTS.md §6.1): for a foreign-currency cash event, use the
``exchange_rates.csv`` row for its settlement date and the stated ``from_currency`` to
``to_currency`` direction. No interpolation, no inversion, no nearest-date fallback. A missing
rate is reported, never guessed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Mapping, Optional

from .models import Currency, ExchangeRate, FinancialEvent, FinancialProfile

RateTable = Mapping[tuple[date, Currency, Currency], ExchangeRate]


class FxRateMissing(LookupError):
    def __init__(self, on: date, from_currency: Currency, to_currency: Currency):
        self.on, self.from_currency, self.to_currency = on, from_currency, to_currency
        super().__init__(f"no exchange rate for {from_currency}->{to_currency} on {on}")


def lookup_rate(rates: RateTable, on: date, from_currency: Currency, to_currency: Currency) -> Decimal:
    """Return the rate for exactly this date and direction, or raise ``FxRateMissing``."""
    if from_currency == to_currency:
        return Decimal(1)
    row = rates.get((on, from_currency, to_currency))
    if row is None:
        raise FxRateMissing(on, from_currency, to_currency)
    return row.rate


def convert(
    amount: Decimal, from_currency: Currency, to_currency: Currency, on: date, rates: RateTable
) -> Decimal:
    """Convert ``amount`` using the dated rate. Result is not rounded; round at the output edge."""
    return amount * lookup_rate(rates, on, from_currency, to_currency)


def event_home_amount(
    event: FinancialEvent, profile: FinancialProfile, rates: RateTable
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """``(home_amount, rate_used)`` for an event.

    * blank amount -> ``(None, None)`` (must be resolved from evidence, never zero)
    * same currency -> ``(amount, 1)``
    * foreign currency -> converted on ``event.effective_date`` (the settlement date when present)

    Raises ``FxRateMissing`` when the dated rate does not exist; the caller decides how to record it.
    """
    if event.amount is None:
        return None, None
    rate = lookup_rate(rates, event.effective_date, event.currency, profile.home_currency)
    return event.amount * rate, rate
