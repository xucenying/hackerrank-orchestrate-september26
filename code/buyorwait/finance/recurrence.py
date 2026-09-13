"""Stage 5 (part) — recurrence detection. Rules: docs/forecast_rules.md §3.

Works on ``Ledger.history`` rows only (settled, before the request, inside the history window).
Produces ``RecurringPattern`` objects with their projected occurrences inside the horizon.
"""

from __future__ import annotations

import calendar
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from ..data.models import Direction, EventType, Flexibility
from .reconciliation import Ledger, ReconciledEvent

# Cadence thresholds (documented in docs/forecast_rules.md §3)
MONTHLY = "monthly"
BIWEEKLY = "biweekly"
WEEKLY = "weekly"
_CADENCES = (
    (MONTHLY, 3, 26, 33),
    (BIWEEKLY, 4, 12, 16),
    (WEEKLY, 4, 5, 9),
)
_GAP_FRACTION = 0.75  # at least this share of gaps must fall in the cadence range
VARIABLE_ESSENTIALS = frozenset({"groceries", "transport", "utilities", "healthcare", "dining", "shopping"})
RECENT_DAYS = 90  # amount estimate window for variable patterns (to calibrate)

# Calibration knobs (see docs/forecast_rules.md; values chosen against the 25 solved samples).
CALIB: dict = {
    "income_mode": "fixed_only",   # none | fixed_only | all
    "amount_stat": "median",       # mean_recent | mean_all | median | max | last
    "include_submonthly": True,    # project weekly / biweekly expense patterns
    "final_keyword_ends_income": True,
    "project_discretionary_variable": True,  # variable-amount patterns outside ESSENTIAL_CATEGORIES and the user's protected list
    "submonthly_as_monthly": False,          # project weekly/biweekly patterns as one monthly lump instead of per occurrence
    "exclude_estimated_amounts": True,       # ignore blank-amount rows (safer estimates) when estimating a pattern amount
    "front_load": "none",                    # none | block_all | block_one : charge projected expenses at the start of each 30-day block
    "front_load_block_days": 30,
    "expense_scale": "1",                    # multiply every projected recurring expense (calibration lever; 1 = off)
    "monthly_anchor": "last",                # last | mode : day-of-month used to project monthly patterns
}
# Essential spending the spec asks to forecast conservatively even when the amount varies.
ESSENTIAL_CATEGORIES = frozenset({
    "groceries", "utilities", "transport", "healthcare", "rent", "housing", "insurance", "education",
    "family_support", "debt_repayment", "childcare",
})
_END_WORDS = ("final", "last ", "terakhir")
SALARY_MAX_CV = 0.10  # monthly income with relative std-dev at or below this is treated as a stable salary


def _salary_level(p: "RecurringPattern") -> Optional[Decimal]:
    """Amount to project for a monthly income pattern, or None when it is not confirmed salary.

    * all occurrences equal                      -> that amount
    * a step change: the last two occurrences equal -> the new level (e.g. a reduced payroll)
    * small variation (CV <= SALARY_MAX_CV)      -> the median
    * otherwise (variable freelance/gig income)  -> None
    """
    amounts = [o.home_amount for o in p.occurrences if o.home_amount is not None]
    if len(amounts) < 3:
        return None
    if len(set(amounts)) == 1:
        return amounts[0]
    if amounts[-1] == amounts[-2]:
        return amounts[-1]
    mean = sum(amounts) / len(amounts)
    if mean > 0:
        var = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        if (var.sqrt() / mean) <= Decimal(str(SALARY_MAX_CV)):
            return Decimal(str(statistics.median([float(a) for a in amounts])))
    return None

_norm_re = re.compile(r"[^a-z ]+")


def normalize_description(s: str) -> str:
    return " ".join(_norm_re.sub(" ", s.lower()).split())


@dataclass
class RecurringPattern:
    key: tuple  # grouping key
    cadence: str
    occurrences: list[ReconciledEvent]  # chronological
    amount: Decimal  # per projected occurrence, home currency
    projected: list[date] = field(default_factory=list)

    @property
    def last(self) -> ReconciledEvent:
        return self.occurrences[-1]

    @property
    def event_id(self) -> str:
        return self.last.event_id

    @property
    def category(self) -> str:
        return self.last.event.category

    @property
    def event_type(self) -> EventType:
        return self.last.event.event_type

    @property
    def direction(self) -> Direction:
        return self.last.direction

    @property
    def is_income(self) -> bool:
        return self.direction is Direction.CREDIT

    @property
    def flexibility(self) -> Flexibility:
        return self.last.event.flexibility

    @property
    def floor(self) -> Optional[Decimal]:
        f = self.last.event.minimum_allowed_amount
        if f is None:
            return None
        fx = self.last.home_amount / self.last.event.amount if self.last.event.amount else Decimal(1)
        return f * fx

    @property
    def description(self) -> str:
        return self.last.event.description


def _detect_cadence(dates: list[date]) -> Optional[str]:
    if len(dates) < 3:
        return None
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    for name, min_n, lo, hi in _CADENCES:
        if len(dates) < min_n:
            continue
        inside = sum(1 for g in gaps if lo <= g <= hi)
        if inside / len(gaps) >= _GAP_FRACTION and lo <= statistics.median(gaps) <= hi:
            return name
    return None


def _amount_for(occ: list[ReconciledEvent], request_date: date) -> Decimal:
    if CALIB["exclude_estimated_amounts"]:
        clean = [o for o in occ if "estimated_amount" not in o.flags]
        if clean:
            occ = clean
    amounts = [o.home_amount for o in occ if o.home_amount is not None]
    if not amounts:
        return Decimal(0)
    if len(set(amounts)) == 1:
        return amounts[0]
    stat = CALIB["amount_stat"]
    if stat == "last":
        return amounts[-1]
    if stat == "max":
        return max(amounts)
    if stat == "median":
        return Decimal(str(statistics.median([float(a) for a in amounts])))
    if stat == "mean_all":
        return sum(amounts) / len(amounts)
    recent = [o.home_amount for o in occ if o.home_amount is not None and o.effective_date >= request_date - timedelta(days=RECENT_DAYS)]
    pool = recent if len(recent) >= 2 else amounts
    return sum(pool) / len(pool)


def _add_months(d: date, months: int, day: int) -> date:
    y, m = d.year, d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def _project(cadence: str, last: date, start: date, end: date, anchor_day: int) -> list[date]:
    out = []
    if cadence == MONTHLY:
        k = 1
        while True:
            d = _add_months(last, k, anchor_day)
            if d > end:
                break
            if d >= start:
                out.append(d)
            k += 1
    else:
        step = 7 if cadence == WEEKLY else 14
        d = last + timedelta(days=step)
        while d <= end:
            if d >= start:
                out.append(d)
            d += timedelta(days=step)
    return out


def detect_patterns(ledger: Ledger) -> list[RecurringPattern]:
    ctx = ledger.ctx
    start, end = ctx.horizon_start, ctx.horizon_end
    hist = [r for r in ledger.history if r.home_amount is not None and r.event.event_type not in (
        EventType.INVESTMENT_PURCHASE, EventType.INVESTMENT_SALE, EventType.INVESTMENT_VALUATION, EventType.REFUND
    ) and r.event.category != "windfall"]
    patterns: list[RecurringPattern] = []
    used: set[str] = set()

    # pass 1: description-level groups
    groups: dict[tuple, list[ReconciledEvent]] = {}
    for r in hist:
        groups.setdefault((r.event.event_type, r.event.category, r.direction, normalize_description(r.event.description)), []).append(r)
    for key, occ in groups.items():
        occ.sort(key=lambda r: (r.effective_date, r.event_id))
        cad = _detect_cadence([o.effective_date for o in occ])
        if cad:
            patterns.append(_make(key, cad, occ, ctx.request.request_date, start, end))
            used.update(o.event_id for o in occ)

    # pass 2: category-level groups for what is left (variable descriptions)
    groups2: dict[tuple, list[ReconciledEvent]] = {}
    for r in hist:
        if r.event_id in used:
            continue
        groups2.setdefault((r.event.event_type, r.event.category, r.direction), []).append(r)
    for key, occ in groups2.items():
        occ.sort(key=lambda r: (r.effective_date, r.event_id))
        cad = _detect_cadence([o.effective_date for o in occ])
        if cad:
            patterns.append(_make(key, cad, occ, ctx.request.request_date, start, end))
            used.update(o.event_id for o in occ)

    patterns = [p for p in patterns if _keep(p, hist, ctx)]
    patterns.sort(key=lambda p: (p.direction.value, p.category, p.event_id))
    return patterns


def _keep(p: RecurringPattern, hist: list[ReconciledEvent], ctx) -> bool:
    if p.is_income:
        mode = CALIB["income_mode"]
        if mode == "none":
            return False
        if CALIB["final_keyword_ends_income"]:
            # any later income row in the same category whose description marks an ending
            later = [r for r in hist if r.direction is Direction.CREDIT and r.event.category == p.category
                     and r.effective_date >= p.last.effective_date and any(w in r.event.description.lower() for w in _END_WORDS)]
            if later:
                return False
        if mode == "fixed_only":
            return p.cadence == MONTHLY and _salary_level(p) is not None
        return True
    if not CALIB["include_submonthly"] and p.cadence != MONTHLY:
        return False
    if not CALIB["project_discretionary_variable"]:
        amounts = {o.home_amount for o in p.occurrences}
        variable = len(amounts) > 1
        essential = p.category in ESSENTIAL_CATEGORIES or p.category in ctx.profile.expense_categories_to_protect
        if variable and not essential:
            return False
    return True


def _make(key, cad, occ, request_date, start, end) -> RecurringPattern:
    last_date = occ[-1].effective_date
    anchor_day = last_date.day if cad == MONTHLY else 0
    if cad == MONTHLY and len(occ) >= 3 and CALIB.get("monthly_anchor", "last") == "mode":
        # most common day-of-month of the last four occurrences (resists one shifted payment,
        # but ignores a genuine date change such as a payroll moved from the 15th to the 23rd)
        days = [o.effective_date.day for o in occ[-4:]]
        anchor_day = max(set(days), key=days.count)
    amount = _amount_for(occ, request_date)
    projected = _project(cad, last_date, start, end, anchor_day)
    if cad != MONTHLY and CALIB["submonthly_as_monthly"] and occ[-1].direction is Direction.DEBIT:
        amount = amount * (Decimal(52) if cad == WEEKLY else Decimal(26)) / Decimal(12)
        projected = _project(MONTHLY, last_date, start, end, last_date.day)
    pattern = RecurringPattern(
        key=key, cadence=cad, occurrences=occ, amount=amount, projected=projected,
    )
    if pattern.is_income and cad == MONTHLY:
        level = _salary_level(pattern)
        if level is not None:
            pattern.amount = level
    return pattern
