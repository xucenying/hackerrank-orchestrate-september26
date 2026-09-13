"""Stage 10 — ExplanationAgent (LLM call #2) with a deterministic template fallback.

The agent receives a ``FactSheet`` built from the final decision and returns ``Explanation``.
It cannot alter any decision field. ``template_explanation`` produces the sample-style sentence
without a model and is used in ``--no-llm`` mode, on model failure, and whenever the validator
rejects the model's text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..data.context import RequestContext
from ..data.models import OutputMethod
from ..finance.plans import FULL_NOW, INSTALLMENTS, PARTIAL, WAIT
from ..infra.formatting import fmt_date_prose, fmt_money_prose, q2
from ..policy.decision_policy import Decision

PROMPT_VERSION = "explanation-v2"  # v2: batching
_PROMPT_PATH = Path(__file__).parent / "prompts" / "explanation_system.md"


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="The decision_explanation sentence(s).")


class ExplanationForRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(description="Copy the request_id of the fact sheet exactly.")
    text: str = Field(description="The decision_explanation for that request.")


class ExplanationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[ExplanationForRequest] = Field(description="Exactly one entry per fact sheet.")


@dataclass
class FactSheet:
    request_id: str
    currency: str
    requested_amount: Decimal
    request_date: str
    deadline: str
    minimum_balance: Decimal
    current_balance: Decimal
    safe_amount: Decimal
    status: str
    method: str
    plan_payments: list[dict]  # [{"date": "8 August 2025", "amount": "IDR 1,000"}]
    n_payments: int
    earliest_date: Optional[str]
    changes: list[dict]  # [{"action": "stop", "description": "...", "new_amount": "USD 23.50"|None}]
    evidence_used: list[str]
    numbers: set[str] = field(default_factory=set)  # every numeric token the explanation may cite

    def to_json(self) -> str:
        d = {k: v for k, v in self.__dict__.items() if k != "numbers"}
        return json.dumps(d, default=str, indent=1)


def _num_tokens(*values) -> set[str]:
    out = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, Decimal):
            v2 = q2(v)
            s = f"{v2:.2f}"
            out.update({s, s.rstrip("0").rstrip("."), str(int(v2)) if v2 == v2.to_integral_value() else s})
        else:
            for tok in str(v).replace(",", "").split():
                t = tok.strip(".")
                if t.replace(".", "", 1).isdigit():
                    out.add(t)
    return out


def build_fact_sheet(ctx: RequestContext, d: Decision, state, descriptions: dict[str, str], evidence_used: list[str]) -> FactSheet:
    req, prof = ctx.request, ctx.profile
    ccy = prof.home_currency.value
    payments = [{"date": fmt_date_prose(p.on), "amount": fmt_money_prose(p.amount, ccy)} for p in (d.plan.payments if d.plan else ())]
    changes = []
    for c in d.changes:
        changes.append({
            "action": c.action, "event_id": c.event_id,
            "description": descriptions.get(c.event_id, c.event_id),
            "new_amount": fmt_money_prose(c.new_amount, ccy) if c.new_amount is not None else None,
        })
    fs = FactSheet(
        request_id=req.request_id, currency=ccy, requested_amount=req.requested_amount,
        request_date=fmt_date_prose(req.request_date), deadline=fmt_date_prose(req.desired_completion_date),
        minimum_balance=prof.minimum_balance_to_keep, current_balance=prof.current_available_balance,
        safe_amount=d.amount_safe_to_pay, status=d.status.value, method=d.method.value,
        plan_payments=payments, n_payments=len(payments),
        earliest_date=fmt_date_prose(d.earliest_date) if d.earliest_date else None,
        changes=changes, evidence_used=evidence_used,
    )
    nums = _num_tokens(req.requested_amount, prof.minimum_balance_to_keep, prof.current_available_balance, d.amount_safe_to_pay,
                       len(payments), req.request_date.day, req.request_date.year, req.desired_completion_date.day, req.desired_completion_date.year, 90)
    for p in (d.plan.payments if d.plan else ()):
        nums |= _num_tokens(p.amount, p.on.day, p.on.year)
    if d.earliest_date:
        nums |= _num_tokens(d.earliest_date.day, d.earliest_date.year)
    for c in d.changes:
        nums |= _num_tokens(c.new_amount)
    fs.numbers = nums
    return fs


# --------------------------------------------------------------------------------------
# Deterministic template (sample style)
# --------------------------------------------------------------------------------------


def template_explanation(d: Decision, ctx: Optional[RequestContext] = None, descriptions: Optional[dict[str, str]] = None) -> str:
    ctx = ctx or getattr(d, "_ctx", None)
    assert ctx is not None, "template_explanation needs the request context"
    req, prof = ctx.request, ctx.profile
    ccy = prof.home_currency.value
    money = lambda x: fmt_money_prose(x, ccy)
    minimum = money(prof.minimum_balance_to_keep)
    descriptions = descriptions or {}
    prefix = ""
    if d.changes:
        parts = []
        for c in d.changes:
            desc = descriptions.get(c.event_id, c.event_id).lower()
            parts.append(f"stop the {desc}" if c.action == "stop" else f"reduce the {desc} to {money(c.new_amount)}")
        prefix = (" and ".join(parts)).capitalize() + ", then "
    plan = d.plan
    if plan is None:
        if d.amount_safe_to_pay > 0:
            return (f"Do not proceed with the {money(req.requested_amount)} request. Although {money(d.amount_safe_to_pay)} is available today, "
                    f"the full amount cannot be completed safely within 90 days.")
        return f"Do not make this payment by {fmt_date_prose(req.desired_completion_date)}. None of the available options keeps the {minimum} minimum protected."
    if plan.kind == FULL_NOW:
        s = f"{prefix}pay {money(req.requested_amount)} today." if prefix else f"Pay {money(req.requested_amount)} today."
        return f"{s} This leaves at least {minimum} available over the next 90 days."
    if plan.kind == WAIT:
        return (f"Pay {money(req.requested_amount)} in full on {fmt_date_prose(plan.first_date)}. "
                f"Paying earlier would take the balance below the {minimum} minimum.")
    if plan.kind == PARTIAL:
        p1, p2 = plan.payments
        return (f"Pay {money(p1.amount)} today and the remaining {money(p2.amount)} on {fmt_date_prose(p2.on)}. "
                f"This completes the full request and keeps the {minimum} minimum protected.")
    if plan.kind == INSTALLMENTS:
        s = f"{prefix}use {plan.n_payments} installments of {money(plan.payments[0].amount)}, starting {fmt_date_prose(plan.first_date)}." if prefix \
            else f"Use {plan.n_payments} installments of {money(plan.payments[0].amount)}, starting {fmt_date_prose(plan.first_date)}."
        return f"{s} This leaves at least {minimum} available."
    return f"Follow the recommended plan. This keeps the {minimum} minimum protected."  # pragma: no cover


# --------------------------------------------------------------------------------------
# Explainers
# --------------------------------------------------------------------------------------


@dataclass
class ExplanationResult:
    text: str
    from_model: bool
    model: Optional[str] = None
    llm_calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: int = 0
    error: Optional[str] = None


class Explainer(Protocol):
    async def explain(self, fs: FactSheet, template: str) -> ExplanationResult: ...


class TemplateExplainer:
    async def explain(self, fs: FactSheet, template: str) -> ExplanationResult:
        return ExplanationResult(text=template, from_model=False)


class PicoExplainer:
    def __init__(self, model_client, *, name: str = "explanation_agent", middlewares: Optional[list] = None):
        from picoagents import Agent

        self.model_name = getattr(model_client, "model", "unknown")
        instructions = _PROMPT_PATH.read_text(encoding="utf-8")
        self.agent = Agent(
            name=name, description="Writes a grounded explanation of an already-made financial decision.",
            instructions=instructions, model_client=model_client, tools=[],
            middlewares=middlewares or [], max_iterations=1, output_format=Explanation,
        )
        self.batch_agent = Agent(
            name=name + "_batch", description="Writes grounded explanations for several already-made decisions.",
            instructions=instructions, model_client=model_client, tools=[],
            middlewares=middlewares or [], max_iterations=1, output_format=ExplanationBatch,
        )

    async def explain_batch(self, items: list[tuple["FactSheet", str]]) -> tuple[dict[str, ExplanationResult], ExplanationResult]:
        """One call for several fact sheets. Returns ``(texts_by_request_id, usage_or_error)``;
        request ids missing from the response are absent and the caller explains them singly."""
        from picoagents.messages import AssistantMessage, UserMessage
        from picoagents.types import AgentResponse, ErrorEvent

        parts = []
        for i, (fs, template) in enumerate(items, 1):
            parts.append(f"=== FACT SHEET {i} of {len(items)}: request_id={fs.request_id} ===\n{fs.to_json()}\nDeterministic draft: {template}")
        task = ("\n\n".join(parts) + "\n\nEach fact sheet is an independent, final decision. Return an ExplanationBatch with exactly one "
                "entry per request_id, each using only the facts and numbers of its own fact sheet.")
        out: dict[str, ExplanationResult] = {}
        try:
            response = None
            last = None
            errors: list[str] = []
            async for item in self.batch_agent.run_stream([UserMessage(content=task, source="pipeline")], stream_tokens=False):
                if isinstance(item, ErrorEvent):
                    errors.append(f"{item.error_type}: {item.error_message}")
                elif isinstance(item, AssistantMessage):
                    last = item
                elif isinstance(item, AgentResponse):
                    response = item
            if errors or response is None:
                raise RuntimeError("batch model call failed: " + " | ".join(errors or ["no response"]))
            batch = None
            if last is not None:
                sc = last.structured_content
                if isinstance(sc, ExplanationBatch):
                    batch = sc
                elif sc is not None:
                    batch = ExplanationBatch.model_validate(sc.model_dump())
                elif last.content:
                    batch = ExplanationBatch.model_validate_json(last.content)
            if batch is None:
                raise ValueError("batch returned no structured output")
            wanted = {fs.request_id for fs, _ in items}
            for entry in batch.results:
                if entry.request_id in wanted and entry.request_id not in out and entry.text.strip():
                    out[entry.request_id] = ExplanationResult(text=entry.text.strip(), from_model=True, model=self.model_name)
            u = response.usage
            return out, ExplanationResult(text="", from_model=True, model=self.model_name, llm_calls=u.llm_calls,
                                          tokens_input=u.tokens_input, tokens_output=u.tokens_output, duration_ms=u.duration_ms)
        except Exception as exc:
            return out, ExplanationResult(text="", from_model=False, model=self.model_name, error=f"{type(exc).__name__}: {exc}")

    async def explain(self, fs: FactSheet, template: str) -> ExplanationResult:
        from picoagents.messages import AssistantMessage, UserMessage
        from picoagents.types import AgentResponse, ErrorEvent

        task = ("Fact sheet (the decision is final):\n" + fs.to_json() +
                "\n\nA deterministic draft in the required style is:\n" + template +
                "\n\nWrite the decision_explanation. Use only facts and numbers from the fact sheet.")
        try:
            response = None
            last = None
            errors: list[str] = []
            async for item in self.agent.run_stream([UserMessage(content=task, source="pipeline")], stream_tokens=False):
                if isinstance(item, ErrorEvent):
                    errors.append(f"{item.error_type}: {item.error_message}")
                elif isinstance(item, AssistantMessage):
                    last = item
                elif isinstance(item, AgentResponse):
                    response = item
            if errors or response is None:
                raise RuntimeError("model call failed: " + " | ".join(errors or ["no response"]))
            text = None
            if last is not None:
                sc = last.structured_content
                if isinstance(sc, Explanation):
                    text = sc.text
                elif sc is not None and hasattr(sc, "text"):
                    text = sc.text
                elif last.content:
                    text = Explanation.model_validate_json(last.content).text
            if not text or not text.strip():
                raise ValueError("empty explanation")
            u = response.usage
            return ExplanationResult(text=text.strip(), from_model=True, model=self.model_name, llm_calls=u.llm_calls,
                                     tokens_input=u.tokens_input, tokens_output=u.tokens_output, duration_ms=u.duration_ms)
        except Exception as exc:
            return ExplanationResult(text=template, from_model=False, model=self.model_name, error=f"{type(exc).__name__}: {exc}")
