"""Stage 11 — final consistency validator with repair ladder. Rules: docs/flow.md stage 11,
CLAUDE.md §18.

``validate_row`` checks a finished ``OutputRow`` against every spec constraint it can verify from
the context, the base path, and the decision. ``finalize`` applies the repair ladder:

1. re-derive derived fields from the plan (status/method/earliest);
2. if the plan itself is invalid, use the next-ranked eligible plan;
3. otherwise the safe fallback row.

Every repair is recorded in ``OutputRow.repairs``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from ..data.context import RequestContext
from ..data.models import AffordabilityStatus, OutputMethod, PaymentMethod
from ..finance.forecast import BalancePath, Payment
from ..finance.plans import FULL_NOW, INSTALLMENTS, PARTIAL, SAFETY_FROM_DAY_ZERO, WAIT, SpendingChange
from ..finance.spending_changes import path_with_changes
from ..finance.state import FinancialState
from ..infra.formatting import fmt_plan, fmt_safe_amount, q2
from .decision_policy import Decision, _map, rank_key

_ALLOWED_PAIRS = {
    (AffordabilityStatus.AFFORDABLE_NOW, OutputMethod.FULL_PAYMENT),
    (AffordabilityStatus.AFFORDABLE_WITH_PLAN, OutputMethod.FULL_PAYMENT),
    (AffordabilityStatus.AFFORDABLE_WITH_PLAN, OutputMethod.PARTIAL_PAYMENT),
    (AffordabilityStatus.AFFORDABLE_WITH_PLAN, OutputMethod.INSTALLMENTS),
    (AffordabilityStatus.AFFORDABLE_LATER, OutputMethod.WAIT),
    (AffordabilityStatus.NOT_AFFORDABLE, OutputMethod.NOT_RECOMMENDED),
}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class OutputRow:
    request_id: str
    amount_safe_to_pay: str
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str
    repairs: list[str] = field(default_factory=list)
    fallback: bool = False

    def as_list(self) -> list[str]:
        return [
            self.request_id, self.amount_safe_to_pay, self.affordability_status, self.recommended_payment_method,
            self.payment_plan, self.earliest_date_for_full_payment, self.spending_changes_needed, self.decision_explanation,
        ]


def row_from_decision(d: Decision, explanation: str) -> OutputRow:
    return OutputRow(
        request_id=d.request_id,
        amount_safe_to_pay=fmt_safe_amount(d.amount_safe_to_pay),
        affordability_status=d.status.value,
        recommended_payment_method=d.method.value,
        payment_plan=fmt_plan(d.plan.payments) if d.plan else "none",
        earliest_date_for_full_payment=d.earliest_date.isoformat() if d.earliest_date else "",
        spending_changes_needed="|".join(c.render() for c in d.changes) if d.changes else "none",
        decision_explanation=explanation,
    )


def fallback_row(request_id: str, safe: Optional[Decimal], currency: str, reason: str) -> OutputRow:
    amt = fmt_safe_amount(max(Decimal(0), safe)) if safe is not None else "0"
    return OutputRow(
        request_id=request_id, amount_safe_to_pay=amt, affordability_status=AffordabilityStatus.NOT_AFFORDABLE.value,
        recommended_payment_method=OutputMethod.NOT_RECOMMENDED.value, payment_plan="none",
        earliest_date_for_full_payment="", spending_changes_needed="none",
        decision_explanation="The request could not be assessed safely from the available records, so no payment is recommended.",
        repairs=[f"fallback: {reason}"], fallback=True,
    )


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def validate_row(row: OutputRow, ctx: RequestContext, d: Decision, state: FinancialState, base_path: BalancePath, fact_numbers: set[str]) -> list[str]:
    req, prof = ctx.request, ctx.profile
    errs: list[str] = []
    safe = Decimal(row.amount_safe_to_pay)
    if not (Decimal(0) <= safe <= req.requested_amount):
        errs.append("safe amount out of bounds")
    try:
        status, method = AffordabilityStatus(row.affordability_status), OutputMethod(row.recommended_payment_method)
    except ValueError:
        return ["invalid enum"]
    if (status, method) not in _ALLOWED_PAIRS:
        errs.append(f"status/method pair {status.value}/{method.value} not allowed")
    if status is AffordabilityStatus.AFFORDABLE_NOW and row.earliest_date_for_full_payment != req.request_date.isoformat():
        errs.append("affordable_now requires earliest == request_date")
    if status is AffordabilityStatus.NOT_AFFORDABLE and (row.payment_plan != "none" or row.spending_changes_needed != "none"):
        errs.append("not_affordable must have plan none and no changes")
    if row.earliest_date_for_full_payment:
        ed = date.fromisoformat(row.earliest_date_for_full_payment)
        if not (base_path.start <= ed <= base_path.date_at(base_path.days)):
            errs.append("earliest date outside horizon")

    # plan structure
    payments = []
    if row.payment_plan != "none":
        for part in row.payment_plan.split("|"):
            ds_, amt = part.split(":")
            payments.append(Payment(date.fromisoformat(ds_), Decimal(amt)))
        if any(payments[i].on >= payments[i + 1].on for i in range(len(payments) - 1)):
            errs.append("plan not chronological")
        if payments[-1].on > req.desired_completion_date:
            errs.append("plan ends after deadline")
    if method is OutputMethod.NOT_RECOMMENDED and payments:
        errs.append("not_recommended with a plan")
    if method in (OutputMethod.FULL_PAYMENT, OutputMethod.WAIT):
        if len(payments) != 1 or abs(payments[0].amount - req.requested_amount) > Decimal("0.01"):
            errs.append("single-payment plan must equal requested amount")
        if method is OutputMethod.FULL_PAYMENT and payments and payments[0].on != req.request_date:
            errs.append("full_payment must be on request_date")
        if not prof.accepts_full_payment:
            errs.append("user does not accept full_payment")
    if method is OutputMethod.PARTIAL_PAYMENT:
        if len(payments) != 2:
            errs.append("partial must have two payments")
        else:
            p1, p2 = payments
            if abs(p1.amount + p2.amount - req.requested_amount) > Decimal("0.01"):
                errs.append("partial payments do not sum to requested")
            if p1.on != req.request_date or abs(p1.amount - safe) > Decimal("0.01"):
                errs.append("partial first payment must be safe amount on request_date")
            if row.earliest_date_for_full_payment != p2.on.isoformat():
                errs.append("partial second payment must be on earliest date")
            if not (Decimal(0) < safe < req.requested_amount):
                errs.append("partial requires 0 < safe < requested")
        if not (req.allows_partial_payment and prof.accepts_partial_payment):
            errs.append("partial not allowed by request/user")
    if method is OutputMethod.INSTALLMENTS:
        if not prof.accepts_installments:
            errs.append("user does not accept installments")
        opts = [o for o in ctx.options if o.payment_method is PaymentMethod.INSTALLMENTS]
        match = any(
            len(o.schedule) == len(payments) and all(pp.on == dd and abs(pp.amount - o.payment_amount) <= Decimal("0.01") for pp, dd in zip(payments, o.schedule))
            for o in opts
        )
        if not match:
            errs.append("installment plan does not match a supplied option")

    # spending changes
    changes: list[SpendingChange] = []
    if row.spending_changes_needed != "none":
        parts = row.spending_changes_needed.split("|")
        if len(parts) > 3:
            errs.append("more than three spending changes")
        seen = set()
        flex_ids = {f.pattern_event_id: f for f in state.flexible}
        for part in parts:
            bits = part.split(":")
            if bits[0] == "stop" and len(bits) == 2:
                changes.append(SpendingChange("stop", bits[1]))
            elif bits[0] == "reduce_to" and len(bits) == 3:
                changes.append(SpendingChange("reduce_to", bits[1], Decimal(bits[2])))
            else:
                errs.append(f"malformed change {part!r}")
                continue
            c = changes[-1]
            if c.event_id in seen:
                errs.append("stop and reduce on the same event")
            seen.add(c.event_id)
            f = flex_ids.get(c.event_id)
            if f is None:
                errs.append(f"change targets non-flexible or unknown event {c.event_id}")
            elif c.action == "stop" and not f.can_stop:
                errs.append(f"stop not permitted for {c.event_id}")
            elif c.action == "reduce_to" and (not f.can_reduce or f.floor is None or c.new_amount is None or c.new_amount < q2(f.floor) - Decimal("0.01")):
                errs.append(f"reduce_to not permitted or below floor for {c.event_id}")

    # safety of the recommended plan on the (possibly modified) path
    if payments and status is not AffordabilityStatus.NOT_AFFORDABLE:
        path = path_with_changes(state, tuple(changes)) if changes else base_path
        try:
            if not path.is_safe(payments, from_date=path.start if SAFETY_FROM_DAY_ZERO else None):
                errs.append("recommended plan breaches the minimum balance")
        except ValueError:
            errs.append("plan payment outside horizon")

    # explanation grounding: every number mentioned must be a known fact
    if not row.decision_explanation.strip():
        errs.append("empty explanation")
    else:
        for tok in _NUM.findall(row.decision_explanation):
            norm = tok.replace(",", "")
            if norm not in fact_numbers and norm.rstrip("0").rstrip(".") not in fact_numbers:
                errs.append(f"explanation cites unknown number {tok}")
                break
    return errs


# --------------------------------------------------------------------------------------
# Repair ladder
# --------------------------------------------------------------------------------------


def finalize(row: OutputRow, ctx: RequestContext, d: Decision, state: FinancialState, base_path: BalancePath,
             fact_numbers: set[str], template_explanation) -> OutputRow:
    errs = validate_row(row, ctx, d, state, base_path, fact_numbers)
    if not errs:
        return row
    # 1. explanation-only problems: swap in the deterministic template
    if all(e.startswith("explanation") or e == "empty explanation" for e in errs):
        row.decision_explanation = template_explanation(d)
        row.repairs.append("explanation replaced by template: " + "; ".join(errs))
        errs = validate_row(row, ctx, d, state, base_path, fact_numbers | numbers_in(row.decision_explanation))
        if not errs:
            return row
    # 2. next-ranked eligible plan
    ranked = sorted((e.plan for e in d.evaluated if e.eligible), key=lambda p: rank_key(p, ctx.request.desired_completion_date))
    for alt in ranked:
        if d.plan is not None and alt == d.plan:
            continue
        status, method = _map(alt)
        d2 = Decision(d.request_id, d.amount_safe_to_pay, status, method, alt, d.earliest_date, d.evaluated, d.path, list(d.notes))
        row2 = row_from_decision(d2, template_explanation(d2))
        row2.repairs = row.repairs + [f"plan replaced by next-ranked {alt.kind}: " + "; ".join(errs)]
        if not validate_row(row2, ctx, d2, state, base_path, fact_numbers | numbers_in(row2.decision_explanation)):
            return row2
    # 3. safe fallback
    fb = fallback_row(ctx.request.request_id, d.amount_safe_to_pay, ctx.profile.home_currency.value, "; ".join(errs))
    fb.repairs = row.repairs + fb.repairs
    return fb


def numbers_in(text: str) -> set[str]:
    out = set()
    for tok in _NUM.findall(text):
        n = tok.replace(",", "")
        out.add(n)
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out
