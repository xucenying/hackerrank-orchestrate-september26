"""Stage 4 — event reconciliation. Rules: docs/event_reconciliation_rules.md.

Input: a ``RequestContext`` and the ``ValidatedEvidence`` produced for each of its evidence
sources. Output: a ``Ledger`` of ``ReconciledEvent`` rows, each with a cash class, a home-currency
amount, an effective date, and provenance, plus the income overrides that apply to the projected
salary stream.

This is the only stage where model output changes a number, and it only does so through
``ValidatedItem`` rows that already passed code validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from ..agents.schemas import ValidatedEvidence, ValidatedItem
from ..data.context import EventView, RequestContext
from ..data.fx import FxRateMissing, lookup_rate
from ..data.models import Currency, Direction, EventStatus, EventType, FinancialEvent

# Cash classes
HISTORY = "history"  # settled before the request; feeds recurrence only
CONFIRMED = "confirmed"  # counted on its date (scheduled salary, settled future rows, evidenced settlements)
RESERVED = "reserved"  # pending/scheduled debit; subtracted on its date
EXCLUDED = "excluded"  # never cash

# Precedence for evidence kinds (lower wins). Spec: explicit cancellation/settlement/amendment
# first, then newer record, then settled beats estimate, then safer reading.
_KIND_PRECEDENCE = {
    "cancellation": 0,
    "settlement": 0,
    "amendment": 0,
    "amount_extraction": 0,
    "delay": 1,
    "duplicate": 1,
    "confirmation": 2,
    "pending_credit": 3,
    "non_cash": 3,
}


@dataclass
class ReconciledEvent:
    event: FinancialEvent
    cash_class: str
    effective_date: date
    home_amount: Optional[Decimal]  # None only while unresolved and no estimate exists
    direction: Direction
    source: str = "dataset"
    edits: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    confirmed_by_evidence: bool = False

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def counts_as_cash(self) -> bool:
        return self.cash_class in (CONFIRMED, RESERVED) and self.home_amount is not None

    @property
    def signed_amount(self) -> Decimal:
        """Positive for credits, negative for debits; zero when not cash."""
        if not self.counts_as_cash:
            return Decimal(0)
        return self.home_amount if self.direction is Direction.CREDIT else -self.home_amount


@dataclass(frozen=True)
class IncomeOverride:
    effective_from: date
    new_amount: Optional[Decimal]  # None => income ends (unless new_day is set: date-only change)
    currency: Optional[Currency]
    source: str
    new_day: Optional[int] = None  # new day-of-month for the payroll from effective_from

    @property
    def ends(self) -> bool:
        return self.new_amount is None and self.new_day is None


@dataclass
class Ledger:
    ctx: RequestContext
    rows: list[ReconciledEvent]
    income_overrides: list[IncomeOverride]
    notes: list[str] = field(default_factory=list)  # human-readable log lines

    @property
    def by_id(self) -> dict[str, ReconciledEvent]:
        return {r.event_id: r for r in self.rows}

    def rows_in(self, cash_class: str) -> list[ReconciledEvent]:
        return [r for r in self.rows if r.cash_class == cash_class]

    @property
    def history(self) -> list[ReconciledEvent]:
        return [r for r in self.rows if r.cash_class == HISTORY and r.effective_date >= self.ctx.history_start]

    @property
    def evidence_used(self) -> list[str]:
        return sorted({r.source for r in self.rows if r.source != "dataset"} | {o.source for o in self.income_overrides})


# --------------------------------------------------------------------------------------
# §1 base classification
# --------------------------------------------------------------------------------------


def _classify(view: EventView, request_date: date) -> str:
    e = view.event
    if e.direction is Direction.NON_CASH or e.status in (EventStatus.FAILED, EventStatus.CANCELLED, EventStatus.UNREALIZED):
        return EXCLUDED
    if e.status is EventStatus.SETTLED:
        return HISTORY if view.effective_date < request_date else CONFIRMED
    if e.status is EventStatus.PENDING:
        return RESERVED if e.direction is Direction.DEBIT else EXCLUDED
    if e.status is EventStatus.SCHEDULED:
        return RESERVED if e.direction is Direction.DEBIT else CONFIRMED
    return EXCLUDED  # pragma: no cover - enum is closed


def _base_rows(ctx: RequestContext) -> list[ReconciledEvent]:
    rows = []
    for v in ctx.events:
        cls = _classify(v, ctx.request.request_date)
        eff = v.effective_date
        if cls == RESERVED and eff < ctx.request.request_date:
            eff = ctx.request.request_date  # past-dated commitment lands on day 0
        r = ReconciledEvent(event=v.event, cash_class=cls, effective_date=eff, home_amount=v.home_amount, direction=v.event.direction)
        if v.fx_missing:
            r.flags.add("fx_missing")
        if v.event.has_blank_amount:
            r.flags.add("blank_amount")
        rows.append(r)
    return rows


# --------------------------------------------------------------------------------------
# §3 blank amounts
# --------------------------------------------------------------------------------------


def _safer_estimate(row: ReconciledEvent, rows: Iterable[ReconciledEvent], ctx: RequestContext) -> Optional[Decimal]:
    """Largest settled same-category amount in the history window, else largest settled debit."""
    if row.direction is Direction.CREDIT:
        return Decimal(0)
    same_cat = [
        r.home_amount for r in rows
        if r.cash_class == HISTORY and r.home_amount is not None and r.event.category == row.event.category
        and r.effective_date >= ctx.history_start and r.event_id != row.event_id
    ]
    if same_cat:
        return max(same_cat)
    any_debit = [r.home_amount for r in rows if r.cash_class == HISTORY and r.home_amount is not None and r.direction is Direction.DEBIT]
    return max(any_debit) if any_debit else None


def _to_home(amount: Decimal, currency: Currency, on: date, ctx: RequestContext) -> Optional[Decimal]:
    try:
        return amount * lookup_rate(ctx.rates, on, currency, ctx.profile.home_currency)
    except FxRateMissing:
        return None


# --------------------------------------------------------------------------------------
# §4 evidence application
# --------------------------------------------------------------------------------------


def _apply_item(row: ReconciledEvent, item: ValidatedItem, ctx: RequestContext, notes: list[str]) -> None:
    kind = item.kind
    src = f"evidence:{item.source_ref}"
    if kind == "cancellation":
        row.cash_class = EXCLUDED
    elif kind == "amendment":
        if item.new_amount is not None:
            ccy = Currency(item.currency) if item.currency else row.event.currency
            home = _to_home(item.new_amount, ccy, item.new_date or row.effective_date, ctx)
            if home is None:
                notes.append(f"{row.event_id}: amendment amount in {ccy} could not be converted; kept original")
                row.flags.add("fx_missing")
            else:
                row.home_amount = home
                row.flags.discard("blank_amount")
        if item.new_date is not None:
            row.effective_date = max(item.new_date, ctx.request.request_date) if row.cash_class == RESERVED else item.new_date
    elif kind == "settlement":
        if row.cash_class in (RESERVED, EXCLUDED) and row.event.status in (EventStatus.PENDING, EventStatus.SCHEDULED):
            row.cash_class = CONFIRMED
            if item.new_date is not None:
                row.effective_date = item.new_date
    elif kind == "delay":
        if item.new_date is not None and item.new_date > row.effective_date:
            row.effective_date = item.new_date
    elif kind == "confirmation":
        row.confirmed_by_evidence = True
    elif kind == "duplicate":
        row.cash_class = EXCLUDED
    elif kind == "amount_extraction":
        if item.new_amount is not None and row.event.has_blank_amount:
            ccy = Currency(item.currency) if item.currency else row.event.currency
            home = _to_home(item.new_amount, ccy, row.effective_date, ctx)
            if home is None:
                notes.append(f"{row.event_id}: extracted amount in {ccy} could not be converted")
                row.flags.add("fx_missing")
            else:
                row.home_amount = home
                row.flags.discard("blank_amount")
                row.flags.add("amount_from_image")
    elif kind in ("pending_credit", "non_cash"):
        pass  # already excluded by §1; recorded for the explanation
    else:  # pragma: no cover
        return
    row.edits.append(kind)
    row.source = src


def reconcile(ctx: RequestContext, evidences: Iterable[ValidatedEvidence]) -> Ledger:
    notes: list[str] = []
    rows = _base_rows(ctx)
    by_id = {r.event_id: r for r in rows}

    # Collect usable items, ordered by precedence then by message time (later wins => applied later).
    src_time = {s.source_id: (s.message.sent_at if s.message else None) for s in ctx.evidence_sources}
    items: list[tuple[int, str, ValidatedItem]] = []
    overrides: list[IncomeOverride] = []
    for ev in evidences:
        for it in ev.usable_items:
            if it.kind == "income_change":
                eff = it.effective_from or it.new_date
                overrides.append(IncomeOverride(eff, it.new_amount, Currency(it.currency) if it.currency else None, f"evidence:{it.source_ref}",
                                                new_day=it.new_date.day if it.new_date else None))
            elif it.kind == "income_end":
                overrides.append(IncomeOverride(it.effective_from, None, None, f"evidence:{it.source_ref}"))
            elif it.target_event_id is not None:
                items.append((_KIND_PRECEDENCE.get(it.kind, 9), (src_time.get(it.source_ref) or date.min).isoformat() if src_time.get(it.source_ref) else "", it))
            else:
                notes.append(f"{it.source_ref}: {it.kind} without target recorded only")
    # Apply lowest-precedence-number last so it wins; within a level, later sent_at wins.
    items.sort(key=lambda t: (-t[0], t[1]))
    applied: dict[str, list[str]] = {}
    for _, _, it in items:
        row = by_id.get(it.target_event_id or "")
        if row is None:
            continue
        # §5: cancellation beats amendment on the same event regardless of order.
        if "cancellation" in applied.get(row.event_id, []) and it.kind == "amendment":
            notes.append(f"{row.event_id}: amendment ignored, event already cancelled")
            continue
        # §4 rule 3: a settled row is never downgraded except by duplicate.
        if row.event.status is EventStatus.SETTLED and it.kind in ("cancellation", "delay", "settlement"):
            notes.append(f"{row.event_id}: {it.kind} ignored on a settled row")
            continue
        _apply_item(row, it, ctx, notes)
        applied.setdefault(row.event_id, []).append(it.kind)

    # §3 blank amounts still unresolved -> safer estimate
    for r in rows:
        if r.home_amount is None and r.event.has_blank_amount and r.cash_class != EXCLUDED:
            est = _safer_estimate(r, rows, ctx)
            r.home_amount = est
            r.flags.add("estimated_amount")
            r.flags.add("unresolved_evidence")
            notes.append(f"{r.event_id}: blank amount not resolved from evidence; safer estimate {est}")

    # income overrides: later sent_at wins -> keep order by effective_from then source order
    overrides.sort(key=lambda o: o.effective_from)
    # income_end excludes scheduled salary rows on/after the date
    for o in overrides:
        if o.ends:
            for r in rows:
                if r.event.event_type is EventType.INCOME and r.cash_class == CONFIRMED and r.effective_date >= o.effective_from:
                    r.cash_class = EXCLUDED
                    r.edits.append("income_end")
                    r.source = o.source

    return Ledger(ctx=ctx, rows=rows, income_overrides=overrides, notes=notes)
