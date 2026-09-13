"""Stage 5 — financial state. Rules: docs/forecast_rules.md §2.

Turns the ledger and detected patterns into the flat inputs the forecast walk needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from ..data.fx import FxRateMissing, lookup_rate
from ..data.models import Direction, EventType, FinancialProfile
from .reconciliation import CONFIRMED, RESERVED, Ledger
from .recurrence import RecurringPattern, detect_patterns

# A projected occurrence within this many days of a reserved/confirmed row of the same pattern
# is treated as the same payment (already in the ledger) and skipped.
DEDUP_DAYS = 3


@dataclass(frozen=True)
class CashFlow:
    on: date
    amount: Decimal  # signed: + credit, - debit
    label: str  # event_id or pattern event_id
    kind: str  # reserved | confirmed | projected_income | projected_expense
    pattern_event_id: Optional[str] = None
    category: Optional[str] = None


@dataclass(frozen=True)
class FlexibleItem:
    pattern_event_id: str
    category: str
    description: str
    can_stop: bool
    can_reduce: bool
    floor: Optional[Decimal]
    per_occurrence: Decimal
    occurrences_in_horizon: int

    @property
    def horizon_saving_if_stopped(self) -> Decimal:
        return self.per_occurrence * self.occurrences_in_horizon

    @property
    def horizon_saving_if_reduced(self) -> Decimal:
        if self.floor is None:
            return Decimal(0)
        return max(Decimal(0), self.per_occurrence - self.floor) * self.occurrences_in_horizon


@dataclass
class FinancialState:
    profile: FinancialProfile
    request_date: date
    horizon_end: date
    starting_balance: Decimal
    minimum_balance: Decimal
    flows: list[CashFlow]  # every dated cash movement in the horizon
    patterns: list[RecurringPattern]
    flexible: list[FlexibleItem]
    notes: list[str] = field(default_factory=list)

    def flows_excluding(self, stops: set[str], reductions: dict[str, Decimal]) -> list[CashFlow]:
        """Flows with spending changes applied to projected expenses only."""
        out = []
        for f in self.flows:
            if f.kind == "projected_expense" and f.pattern_event_id:
                if f.pattern_event_id in stops:
                    continue
                if f.pattern_event_id in reductions:
                    out.append(CashFlow(f.on, -reductions[f.pattern_event_id], f.label, f.kind, f.pattern_event_id, f.category))
                    continue
            out.append(f)
        return out


def build_state(ledger: Ledger) -> FinancialState:
    ctx = ledger.ctx
    prof = ctx.profile
    flows: list[CashFlow] = []
    notes: list[str] = []

    # ledger rows that are cash inside the horizon
    for r in ledger.rows:
        if r.cash_class in (RESERVED, CONFIRMED) and r.home_amount is not None and ctx.horizon_start <= r.effective_date <= ctx.horizon_end:
            flows.append(CashFlow(r.effective_date, r.signed_amount, r.event_id, "reserved" if r.cash_class == RESERVED else "confirmed", category=r.event.category))

    patterns = detect_patterns(ledger)
    from .recurrence import normalize_description

    ledger_rows = [(f.on, f.category, f.amount < 0, normalize_description(ledger.by_id[f.label].event.description)) for f in flows]

    def already_in_ledger(d: date, category: str, is_debit: bool, desc: str = "") -> bool:
        """A projected occurrence is the same payment as a reserved/confirmed row only when the row
        is in the same category and direction, within DEDUP_DAYS, and either has the same
        normalized description or is salary (the scheduled "Next confirmed salary" row)."""
        nd = normalize_description(desc)
        for ld, lc, ldeb, ldesc in ledger_rows:
            if abs((d - ld).days) <= DEDUP_DAYS and lc == category and ldeb == is_debit and (category == "salary" or ldesc == nd):
                return True
        return False

    flexible: list[FlexibleItem] = []
    for p in patterns:
        is_debit = p.direction is Direction.DEBIT
        n = 0
        projected = _shift_salary_days(p, ledger) if p.is_income else p.projected
        for d in projected:
            if already_in_ledger(d, p.category, is_debit, p.description):
                continue
            amt = p.amount
            if p.is_income:
                amt = _income_amount_on(d, p, ledger, notes)
                if amt is None:
                    continue
                flows.append(CashFlow(d, amt, p.event_id, "projected_income", p.event_id, p.category))
            else:
                flows.append(CashFlow(d, -amt, p.event_id, "projected_expense", p.event_id, p.category))
            n += 1
        if not p.is_income and p.flexibility.value != "fixed" and n > 0:
            cat = p.category
            protected = cat in prof.expense_categories_to_protect
            flexible.append(FlexibleItem(
                pattern_event_id=p.event_id, category=cat, description=p.description,
                can_stop=p.flexibility.can_stop and cat in prof.expense_categories_user_is_willing_to_stop and not protected,
                can_reduce=p.flexibility.can_reduce and cat in prof.expense_categories_user_is_willing_to_reduce and not protected and p.floor is not None,
                floor=p.floor, per_occurrence=p.amount, occurrences_in_horizon=n,
            ))

    # A scheduled "Next confirmed salary" row confirms the salary stream: when no fixed-amount
    # payroll pattern was detected from history, project it forward monthly at that amount.
    if not any(p.is_income for p in patterns):
        sched = [r for r in ledger.rows if r.cash_class == CONFIRMED and r.event.event_type is EventType.INCOME
                 and r.event.category == "salary" and r.event.status.value == "scheduled" and r.home_amount is not None]
        for r in sched:
            k = 1
            while True:
                d = _add_months_safe(r.effective_date, k)
                if d > ctx.horizon_end:
                    break
                amt = _income_override_amount(d, r.home_amount, ledger)
                if amt is not None and not already_in_ledger(d, "salary", False):
                    flows.append(CashFlow(d, amt, r.event_id, "projected_income", r.event_id, "salary"))
                k += 1
            notes.append(f"{r.event_id}: scheduled salary projected monthly through the horizon")

    # apply income overrides to confirmed scheduled salary rows too (amendment of the stream)
    for o in ledger.income_overrides:
        if o.ends or o.new_amount is None:
            continue
        for i, f in enumerate(flows):
            if f.kind == "confirmed" and f.amount > 0 and f.on >= o.effective_from:
                row = ledger.by_id.get(f.label)
                if row is not None and row.event.event_type is EventType.INCOME and row.event.category == "salary":
                    home = _convert(o.new_amount, o.currency, f.on, ledger)
                    if home is not None:
                        flows[i] = CashFlow(f.on, home, f.label, f.kind, category=f.category)
                        notes.append(f"{f.label}: scheduled salary amended to {home} by {o.source}")

    flows = _front_load(flows, ctx.request.request_date)
    from .recurrence import CALIB as _C
    scale = Decimal(str(_C.get("expense_scale", "1")))
    if scale != 1:
        flows = [CashFlow(f.on, f.amount * scale, f.label, f.kind, f.pattern_event_id, f.category) if f.kind == "projected_expense" else f for f in flows]
    flows.sort(key=lambda f: (f.on, f.kind, f.label))
    return FinancialState(
        profile=prof, request_date=ctx.request.request_date, horizon_end=ctx.horizon_end,
        starting_balance=prof.current_available_balance, minimum_balance=prof.minimum_balance_to_keep,
        flows=flows, patterns=patterns, flexible=flexible, notes=notes,
    )


def _front_load(flows: list[CashFlow], request_date: date) -> list[CashFlow]:
    """Optional convention (docs/forecast_rules.md §12): projected expenses are charged at the start
    of the 30-day block they fall in, i.e. before that block's income. ``block_all`` keeps every
    occurrence; ``block_one`` keeps one occurrence per pattern per block."""
    from .recurrence import CALIB

    mode = CALIB.get("front_load", "none")
    if mode == "none":
        return flows
    block = int(CALIB.get("front_load_block_days", 30))
    out: list[CashFlow] = []
    seen: set[tuple] = set()
    for f in flows:
        if f.kind != "projected_expense":
            out.append(f)
            continue
        k = (f.on - request_date).days // block
        start = request_date + timedelta(days=k * block)
        if mode == "block_one":
            key = (f.pattern_event_id, k)
            if key in seen:
                continue
            seen.add(key)
        out.append(CashFlow(start, f.amount, f.label, f.kind, f.pattern_event_id, f.category))
    return out


def _shift_salary_days(p: RecurringPattern, ledger: Ledger) -> list[date]:
    """Apply date-only income overrides: projected salary dates on/after effective_from move to
    the new day-of-month (clamped to month length)."""
    import calendar

    days = [o for o in ledger.income_overrides if o.new_day is not None and p.category == "salary"]
    if not days:
        return p.projected
    out = []
    for d in p.projected:
        nd = d
        for o in sorted(days, key=lambda x: x.effective_from):
            if d >= o.effective_from:
                nd = date(d.year, d.month, min(o.new_day, calendar.monthrange(d.year, d.month)[1]))
        out.append(nd)
    return sorted(set(out))


def _add_months_safe(d: date, months: int) -> date:
    import calendar
    y, m = d.year, d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _income_override_amount(d: date, base: Decimal, ledger: Ledger) -> Optional[Decimal]:
    amt: Optional[Decimal] = base
    for o in ledger.income_overrides:
        if d >= o.effective_from:
            if o.ends:
                amt = None
            elif o.new_amount is not None:
                amt = _convert(o.new_amount, o.currency, d, ledger) or amt
    return amt


def _convert(amount: Decimal, currency, on: date, ledger: Ledger) -> Optional[Decimal]:
    home_ccy = ledger.ctx.profile.home_currency
    if currency is None or currency == home_ccy:
        return amount
    try:
        return amount * lookup_rate(ledger.ctx.rates, on, currency, home_ccy)
    except FxRateMissing:
        # A projected date has no settlement row: use the latest dated rate on or before it
        # (documented deviation, future evidence-supplied flows only).
        prior = [k for k in ledger.ctx.rates if k[1] == currency and k[2] == home_ccy and k[0] <= on]
        if prior:
            return amount * ledger.ctx.rates[max(prior)].rate
        ledger.notes.append(f"income override in {currency} on {on}: no rate on or before; ignored")
        return None


def _income_amount_on(d: date, p: RecurringPattern, ledger: Ledger, notes: list[str]) -> Optional[Decimal]:
    amt: Optional[Decimal] = p.amount
    for o in ledger.income_overrides:  # sorted by effective_from
        if d >= o.effective_from and p.category == "salary":
            if o.ends:
                amt = None
            elif o.new_amount is not None:
                conv = _convert(o.new_amount, o.currency, d, ledger)
                amt = conv if conv is not None else amt
    return amt
