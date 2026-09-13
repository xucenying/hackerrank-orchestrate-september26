"""Stages 7 and 8 — candidate plan generation and evaluation. Rules: docs/payment_plan_rules.md §1-§3."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from ..data.context import RequestContext
from ..data.models import PaymentMethod, PaymentOption
from .forecast import BalancePath, Payment

FULL_NOW = "full_now"
WAIT = "wait"
PARTIAL = "partial"
INSTALLMENTS = "installments"

_MONEY_TOL = Decimal("0.01")
DAYS_PER_MONTH = 30  # documented in docs/payment_plan_rules.md §3.2
# Spec: "the balance must never fall below minimum_balance_to_keep after any projected essential
# expense or payment in the recommended plan" -> judge the whole horizon (day 0 onward).
SAFETY_FROM_DAY_ZERO = True


@dataclass(frozen=True)
class SpendingChange:
    action: str  # stop | reduce_to
    event_id: str
    new_amount: Optional[Decimal] = None

    def render(self) -> str:
        from ..infra.formatting import fmt_plan_amount

        if self.action == "stop":
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{fmt_plan_amount(self.new_amount or Decimal(0))}"


@dataclass(frozen=True)
class Plan:
    kind: str
    payments: tuple[Payment, ...]
    option: Optional[PaymentOption] = None
    changes: tuple[SpendingChange, ...] = ()

    @property
    def total_paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal(0))

    @property
    def first_date(self) -> date:
        return self.payments[0].on

    @property
    def last_date(self) -> date:
        return self.payments[-1].on

    @property
    def n_payments(self) -> int:
        return len(self.payments)

    @property
    def option_rank(self) -> tuple[int, str]:
        if self.option is None:
            return (-1, "")
        suffix = self.option.payment_option_id.rsplit("_", 1)[-1]
        return (int(suffix) if suffix.isdigit() else 10**9, self.option.payment_option_id)

    def with_changes(self, changes: tuple[SpendingChange, ...]) -> "Plan":
        return Plan(self.kind, self.payments, self.option, changes)


@dataclass
class EvaluatedPlan:
    plan: Plan
    eligible: bool
    reason: Optional[str] = None  # first failing gate
    breach_date: Optional[date] = None  # when the safety gate failed
    completes_by_deadline: bool = True


# --------------------------------------------------------------------------------------
# Generation (stage 7)
# --------------------------------------------------------------------------------------


def generate_plans(ctx: RequestContext, safe: Decimal, earliest: Optional[date]) -> list[Plan]:
    req = ctx.request
    plans: list[Plan] = []
    full_opt = next((o for o in ctx.options if o.payment_method is PaymentMethod.FULL_PAYMENT), None)
    plans.append(Plan(FULL_NOW, (Payment(req.request_date, req.requested_amount),), full_opt))
    if earliest is not None and earliest > req.request_date:
        plans.append(Plan(WAIT, (Payment(earliest, req.requested_amount),)))
    if earliest is not None and Decimal(0) < safe < req.requested_amount and earliest > req.request_date:
        plans.append(Plan(PARTIAL, (Payment(req.request_date, safe), Payment(earliest, req.requested_amount - safe))))
    for o in ctx.options:
        if o.payment_method is PaymentMethod.INSTALLMENTS:
            plans.append(Plan(INSTALLMENTS, tuple(Payment(d, o.payment_amount) for d in o.schedule), o))
    return plans


# --------------------------------------------------------------------------------------
# Gates (stage 8)
# --------------------------------------------------------------------------------------


def option_months(o: PaymentOption) -> int:
    if o.number_of_payments <= 1 or not o.payment_frequency_days:
        return 1
    return max(1, math.ceil(o.number_of_payments * o.payment_frequency_days / DAYS_PER_MONTH))


def evaluate_plan(plan: Plan, ctx: RequestContext, path: BalancePath) -> EvaluatedPlan:
    req, prof = ctx.request, ctx.profile
    k = plan.kind

    # 1. preference
    if k in (FULL_NOW, WAIT) and not prof.accepts_full_payment:
        return EvaluatedPlan(plan, False, "user does not accept full_payment")
    if k == PARTIAL and not prof.accepts_partial_payment:
        return EvaluatedPlan(plan, False, "user does not accept partial_payment")
    if k == INSTALLMENTS and not prof.accepts_installments:
        return EvaluatedPlan(plan, False, "user does not accept installments")

    # 2. request / limit gates
    if k == PARTIAL and not req.allows_partial_payment:
        return EvaluatedPlan(plan, False, "request does not allow partial payment")
    if k == INSTALLMENTS:
        o = plan.option
        assert o is not None
        if prof.max_installment_months is not None and option_months(o) > prof.max_installment_months:
            return EvaluatedPlan(plan, False, f"option spans {option_months(o)} months > max {prof.max_installment_months}")
        if abs(o.payment_amount * o.number_of_payments - o.total_payable_amount) > _MONEY_TOL:
            return EvaluatedPlan(plan, False, "option arithmetic inconsistent")
        if any(plan.payments[i].on >= plan.payments[i + 1].on for i in range(len(plan.payments) - 1)):
            return EvaluatedPlan(plan, False, "option schedule not chronological")

    # 3. deadline
    completes = plan.last_date <= req.desired_completion_date
    if not completes:
        return EvaluatedPlan(plan, False, f"last payment {plan.last_date} after deadline {req.desired_completion_date}", completes_by_deadline=False)

    # 4. horizon
    if plan.last_date > path.date_at(path.days) or plan.first_date < path.start:
        return EvaluatedPlan(plan, False, "payment outside the forecast horizon")

    # 5. safety
    breach = path.first_breach(plan.payments, from_date=path.start if SAFETY_FROM_DAY_ZERO else None)
    if breach is not None:
        return EvaluatedPlan(plan, False, f"balance below minimum on {breach}", breach_date=breach)
    return EvaluatedPlan(plan, True)
