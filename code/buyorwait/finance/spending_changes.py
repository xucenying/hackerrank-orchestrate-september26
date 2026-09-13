"""Spending-change search. Rules: docs/payment_plan_rules.md §5.

Only tried for a plan that failed **only** the safety gate. Returns the first combination
(fewest changes, stops before reductions, largest saving first) that makes the plan safe.
"""

from __future__ import annotations

from itertools import combinations
from typing import Optional

from ..data.context import RequestContext
from ..infra.formatting import q2
from .forecast import BalancePath, build_path
from .plans import EvaluatedPlan, Plan, SpendingChange, evaluate_plan
from .state import FinancialState, FlexibleItem

MAX_CHANGES = 3  # spec: up to three changes


def _candidates(state: FinancialState) -> list[SpendingChange]:
    out: list[tuple[int, object, SpendingChange]] = []
    for f in state.flexible:
        if f.can_stop:
            out.append((0, -f.horizon_saving_if_stopped, SpendingChange("stop", f.pattern_event_id)))
        if f.can_reduce and f.floor is not None and f.floor < f.per_occurrence:
            out.append((1, -f.horizon_saving_if_reduced, SpendingChange("reduce_to", f.pattern_event_id, q2(f.floor))))
    out.sort(key=lambda t: (t[0], t[1], t[2].event_id))
    return [c for _, _, c in out]


def path_with_changes(state: FinancialState, changes: tuple[SpendingChange, ...]) -> BalancePath:
    stops = {c.event_id for c in changes if c.action == "stop"}
    reductions = {c.event_id: c.new_amount for c in changes if c.action == "reduce_to" and c.new_amount is not None}
    return build_path(state, state.flows_excluding(stops, reductions))


def find_changes(plan: Plan, ctx: RequestContext, state: FinancialState) -> Optional[tuple[EvaluatedPlan, BalancePath]]:
    cands = _candidates(state)
    if not cands:
        return None
    for n in range(1, MAX_CHANGES + 1):
        for combo in combinations(cands, n):
            ids = [c.event_id for c in combo]
            if len(set(ids)) != len(ids):  # never stop and reduce the same event
                continue
            path = path_with_changes(state, combo)
            ev = evaluate_plan(plan.with_changes(combo), ctx, path)
            if ev.eligible:
                return ev, path
    return None
