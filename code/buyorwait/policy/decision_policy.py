"""Stage 9 — ranking and decision mapping. Rules: docs/payment_plan_rules.md §4 and §6.

``decide`` is a pure function of the forecast outputs, the plans, and the profile. No model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from ..data.context import RequestContext
from ..data.models import AffordabilityStatus, OutputMethod
from ..finance.forecast import BalancePath
from ..finance.plans import FULL_NOW, INSTALLMENTS, PARTIAL, WAIT, EvaluatedPlan, Plan, evaluate_plan, generate_plans
from ..finance.spending_changes import find_changes
from ..finance.state import FinancialState


def rank_key(p: Plan, deadline: date):
    return (
        0 if p.last_date <= deadline else 1,  # 1. completes by deadline
        len(p.changes),  # 2. no spending changes
        p.total_paid,  # 3. total amount paid
        p.first_date,  # 4. start earlier
        p.n_payments,  # 5. fewer payments
        p.option_rank,  # 6. lowest option id (constructed plans first)
    )


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: Decimal
    status: AffordabilityStatus
    method: OutputMethod
    plan: Optional[Plan]  # None => payment_plan "none"
    earliest_date: Optional[date]
    evaluated: list[EvaluatedPlan] = field(default_factory=list)  # every candidate with its verdict
    path: Optional[BalancePath] = None  # path used for the winning plan (with changes if any)
    notes: list[str] = field(default_factory=list)

    @property
    def changes(self):
        return self.plan.changes if self.plan else ()


def _map(plan: Optional[Plan]) -> tuple[AffordabilityStatus, OutputMethod]:
    if plan is None:
        return AffordabilityStatus.NOT_AFFORDABLE, OutputMethod.NOT_RECOMMENDED
    if plan.kind == FULL_NOW:
        return (AffordabilityStatus.AFFORDABLE_NOW if not plan.changes else AffordabilityStatus.AFFORDABLE_WITH_PLAN), OutputMethod.FULL_PAYMENT
    if plan.kind == PARTIAL:
        return AffordabilityStatus.AFFORDABLE_WITH_PLAN, OutputMethod.PARTIAL_PAYMENT
    if plan.kind == INSTALLMENTS:
        return AffordabilityStatus.AFFORDABLE_WITH_PLAN, OutputMethod.INSTALLMENTS
    if plan.kind == WAIT:
        return AffordabilityStatus.AFFORDABLE_LATER, OutputMethod.WAIT
    raise ValueError(plan.kind)  # pragma: no cover


def decide(ctx: RequestContext, state: FinancialState, base_path: BalancePath, safe: Decimal, earliest: Optional[date]) -> Decision:
    req = ctx.request
    plans = generate_plans(ctx, safe, earliest)
    evaluated: list[EvaluatedPlan] = []
    winners: list[tuple[Plan, BalancePath]] = []
    for p in plans:
        ev = evaluate_plan(p, ctx, base_path)
        evaluated.append(ev)
        if ev.eligible:
            winners.append((p, base_path))
        elif ev.breach_date is not None and p.kind != WAIT:
            # failed only the safety gate: try permitted spending changes
            found = find_changes(p, ctx, state)
            if found is not None:
                ev2, path2 = found
                evaluated.append(ev2)
                winners.append((ev2.plan, path2))
    winners.sort(key=lambda t: rank_key(t[0], req.desired_completion_date))
    plan, path = (winners[0] if winners else (None, base_path))
    status, method = _map(plan)
    d = Decision(
        request_id=req.request_id, amount_safe_to_pay=max(Decimal(0), min(safe, req.requested_amount)),
        status=status, method=method, plan=plan, earliest_date=earliest, evaluated=evaluated, path=path,
    )
    if plan is None:
        d.notes.append("no eligible plan: " + "; ".join(f"{e.plan.kind}{'('+e.plan.option.payment_option_id+')' if e.plan.option else ''}: {e.reason}" for e in evaluated))
    return d
