"""Stage 6 — the 90-day balance path and its derived quantities. Rules: docs/forecast_rules.md §4-§7."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from .state import CashFlow, FinancialState


@dataclass(frozen=True)
class Payment:
    on: date
    amount: Decimal


@dataclass
class BalancePath:
    start: date
    minimum: Decimal
    balances: list[Decimal]  # index = days since start, inclusive of day 0 .. horizon

    @property
    def days(self) -> int:
        return len(self.balances) - 1

    def index(self, d: date) -> int:
        i = (d - self.start).days
        if i < 0 or i > self.days:
            raise ValueError(f"{d} outside the forecast horizon")
        return i

    def date_at(self, i: int) -> date:
        return self.start + timedelta(days=i)

    def headroom(self, d: date) -> Decimal:
        """min over t >= d of B(t) - minimum."""
        i = self.index(d)
        return min(self.balances[i:]) - self.minimum

    def with_payments(self, payments: Iterable[Payment]) -> "BalancePath":
        out = list(self.balances)
        for p in payments:
            i = self.index(p.on)
            for t in range(i, len(out)):
                out[t] -= p.amount
        return BalancePath(self.start, self.minimum, out)

    def is_safe(self, payments: Sequence[Payment] = (), from_date: Optional[date] = None) -> bool:
        """True when the balance never drops below the minimum on any day from ``from_date``
        (default: the first payment date, or day 0 when there are no payments). A payment only
        affects days on or after its date, so an earlier projected breach is not attributed to it."""
        return self.first_breach(payments, from_date) is None

    def first_breach(self, payments: Sequence[Payment] = (), from_date: Optional[date] = None) -> Optional[date]:
        path = self.with_payments(payments) if payments else self
        if from_date is None:
            from_date = min((p.on for p in payments), default=self.start)
        start_i = self.index(from_date)
        for i in range(start_i, len(path.balances)):
            if path.balances[i] < self.minimum:
                return self.date_at(i)
        return None

    @property
    def low_point(self) -> tuple[date, Decimal]:
        i = min(range(len(self.balances)), key=lambda k: (self.balances[k], k))
        return self.date_at(i), self.balances[i]


def build_path(state: FinancialState, flows: Optional[Sequence[CashFlow]] = None) -> BalancePath:
    flows = state.flows if flows is None else flows
    n = (state.horizon_end - state.request_date).days
    daily = [Decimal(0)] * (n + 1)
    for f in flows:
        i = (f.on - state.request_date).days
        if 0 <= i <= n:
            daily[i] += f.amount
    bal = []
    running = state.starting_balance
    for i in range(n + 1):
        running += daily[i]
        bal.append(running)
    return BalancePath(state.request_date, state.minimum_balance, bal)


def safe_amount(path: BalancePath, requested: Decimal) -> Decimal:
    h = path.headroom(path.start)
    return max(Decimal(0), min(h, requested))


def earliest_full_payment_date(path: BalancePath, requested: Decimal) -> Optional[date]:
    """First day d in the horizon on which paying ``requested`` as one payment passes the
    90-day safety check (balance never below the minimum on any day of the horizon), with no
    spending changes and regardless of payment preferences. ``None`` when no such day exists.

    Because a payment on d only lowers days >= d, this is equivalent to: the base path never
    breaches before d, and ``headroom(d) >= requested``.
    """
    if path.balances[0] < path.minimum:
        return None
    for i in range(path.days + 1):
        d = path.date_at(i)
        if i > 0 and path.balances[i - 1] < path.minimum:
            return None  # the base path breaches before any later payment could be made
        if path.headroom(d) >= requested:
            return d
    return None
