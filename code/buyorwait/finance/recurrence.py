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
INTERVAL = "interval"  # any other consistent spacing, e.g. the dataset's 10-day and 21-day rhythms
_CADENCES = (
    (MONTHLY, 3, 26, 33),
    (BIWEEKLY, 4, 12, 16),
    (WEEKLY, 4, 5, 9),
)
_GAP_FRACTION = 0.75  # at least this share of gaps must fall in the cadence range
# Generic-interval detection. The named buckets above miss real rhythms in this dataset:
# groceries every ~10 days (128 patterns) and dining/transport every ~21 days (148 patterns)
# fall between weekly and monthly and were dropped entirely, under-projecting spending.
INTERVAL_MIN_DAYS = 4
INTERVAL_MAX_DAYS = 45
INTERVAL_MIN_OCCURRENCES = 3
INTERVAL_TOLERANCE = 0.35  # a gap counts as "on rhythm" within +/- 35% of the median gap
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
    "salary_wobble_stat": "median",          # median | min : level projected for a salary that varies within SALARY_MAX_CV
    "stable_salary_rescue": True,            # project a salary whose amounts repeat on a fixed day-of-month even when
                                             # the cadence test fails (a missed month) or only two payslips exist
    "stable_salary_min_occurrences": 2,
    "generic_interval": False,               # detect any consistent spacing (10-day, 21-day) the named cadences miss;
                                             # tested 2026-09-13 and rejected, see docs/forecast_rules.md §12
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
            if CALIB.get("salary_wobble_stat", "median") == "min":
                return min(amounts)  # conservative: assume the lowest observed payslip recurs
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


def _detect_cadence(dates: list[date], allow_interval: bool = True) -> Optional[str]:
    """Cadence of a series. ``allow_interval`` is False for description-level groups: a generic
    interval there would split one category (groceries under three rotating shop names) into
    several sub-patterns, which fragments the projection. Intervals are only detected once the
    whole category is grouped together."""
    if len(dates) < 3:
        return None
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    for name, min_n, lo, hi in _CADENCES:
        if len(dates) < min_n:
            continue
        inside = sum(1 for g in gaps if lo <= g <= hi)
        if inside / len(gaps) >= _GAP_FRACTION and lo <= statistics.median(gaps) <= hi:
            return name
    if allow_interval and CALIB.get("generic_interval", True):
        return INTERVAL if _interval_days(dates) else None
    return None


def _interval_days(dates: list[date]) -> Optional[int]:
    """Median spacing of a series that repeats on a consistent rhythm the named cadences miss."""
    if len(dates) < INTERVAL_MIN_OCCURRENCES:
        return None
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not gaps:
        return None
    med = statistics.median(gaps)
    if not (INTERVAL_MIN_DAYS <= med <= INTERVAL_MAX_DAYS):
        return None
    tol = max(2.0, med * INTERVAL_TOLERANCE)
    if sum(1 for g in gaps if abs(g - med) <= tol) / len(gaps) < _GAP_FRACTION:
        return None
    return int(round(med))


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


def _project(cadence: str, last: date, start: date, end: date, anchor_day: int, interval: Optional[int] = None) -> list[date]:
    out = []
    if cadence == INTERVAL:
        step = interval or 0
        if step <= 0:
            return out
        d = last + timedelta(days=step)
        while d <= end:
            if d >= start:
                out.append(d)
            d += timedelta(days=step)
        return out
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


def _stable_salary_pattern(hist: list[ReconciledEvent], ctx) -> Optional[RecurringPattern]:
    """Rescue a monthly salary that the generic cadence test rejects.

    The dataset contains payrolls that pay the same amount on the same day of the month but
    that the cadence test drops: a missed month (unpaid leave, e.g. March, April, then July)
    makes a 91-day gap, and a new job supplies only two payslips. Both are unambiguous salaries,
    and dropping them leaves the user with no income at all for 90 days, which is never right.

    Conditions (deliberately strict): every occurrence has the same home-currency amount, the
    same day-of-month, category ``salary``, and there are at least
    ``stable_salary_min_occurrences`` of them.
    """
    sal = [r for r in hist if r.direction is Direction.CREDIT and r.event.category == "salary" and r.home_amount is not None]
    if len(sal) < int(CALIB.get("stable_salary_min_occurrences", 2)):
        return None
    if len({r.home_amount for r in sal}) != 1 or len({r.effective_date.day for r in sal}) != 1:
        return None
    if CALIB["final_keyword_ends_income"] and any(w in r.event.description.lower() for r in sal for w in _END_WORDS):
        return None
    sal.sort(key=lambda r: (r.effective_date, r.event_id))
    last = sal[-1]
    key = (last.event.event_type, "salary", Direction.CREDIT, "stable_salary")
    return RecurringPattern(
        key=key, cadence=MONTHLY, occurrences=sal, amount=sal[0].home_amount,
        projected=_project(MONTHLY, last.effective_date, ctx.horizon_start, ctx.horizon_end, last.effective_date.day),
    )


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
        cad = _detect_cadence([o.effective_date for o in occ], allow_interval=False)
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
    if CALIB.get("stable_salary_rescue", True) and not any(p.is_income and p.category == "salary" for p in patterns):
        rescued = _stable_salary_pattern(hist, ctx)
        if rescued is not None:
            patterns.append(rescued)
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
    interval = _interval_days([o.effective_date for o in occ]) if cad == INTERVAL else None
    projected = _project(cad, last_date, start, end, anchor_day, interval)
    if cad != MONTHLY and CALIB["submonthly_as_monthly"] and occ[-1].direction is Direction.DEBIT:
        per_year = {WEEKLY: Decimal(52), BIWEEKLY: Decimal(26)}.get(cad, Decimal(365) / Decimal(interval or 30))
        amount = amount * per_year / Decimal(12)
        projected = _project(MONTHLY, last_date, start, end, last_date.day)
    pattern = RecurringPattern(
        key=key, cadence=cad, occurrences=occ, amount=amount, projected=projected,
    )
    if pattern.is_income and cad == MONTHLY:
        level = _salary_level(pattern)
        if level is not None:
            pattern.amount = level
    return pattern
