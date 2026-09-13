"""Tests for stages 4-11: reconciliation, recurrence, forecast, plans, policy, consistency, pipeline.

All deterministic; no network. Uses the synthetic dataset from conftest plus purpose-built rows.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from buyorwait.agents.schemas import ValidatedEvidence, ValidatedItem
from buyorwait.data import load_dataset
from buyorwait.data.context import build_context
from buyorwait.finance.forecast import BalancePath, Payment, build_path, earliest_full_payment_date, safe_amount
from buyorwait.finance.plans import FULL_NOW, INSTALLMENTS, PARTIAL, WAIT, evaluate_plan, generate_plans
from buyorwait.finance.reconciliation import CONFIRMED, EXCLUDED, HISTORY, RESERVED, reconcile
from buyorwait.finance.recurrence import detect_patterns
from buyorwait.finance.state import build_state
from buyorwait.infra.formatting import fmt_plan_amount, fmt_safe_amount
from buyorwait.infra.usage import UsageLedger, build_usage_report
from buyorwait.pipeline import Pipeline, PipelineConfig
from buyorwait.policy.consistency import OutputRow, fallback_row, finalize, row_from_decision, validate_row
from buyorwait.policy.decision_policy import decide
from buyorwait.agents.explanation_agent import build_fact_sheet, template_explanation
from conftest import SYNTHETIC, write_synthetic

D = Decimal


def _item(kind, target=None, amount=None, currency=None, new_date=None, eff=None, src="message_1", conf="high", reason=None):
    return ValidatedItem(kind=kind, target_event_id=target, new_amount=D(str(amount)) if amount is not None else None,
                         currency=currency, new_date=new_date, effective_from=eff, confidence=conf, source_ref=src,
                         quote="q", original_kind=kind, rejection_reason=reason)


def _ev(source_id, *items):
    return ValidatedEvidence(source_id=source_id, items=tuple(items), injection_detected=False, summary="")


# --------------------------------------------------------------------------------------
# A richer synthetic user: monthly rent + salary, weekly groceries, a stoppable subscription
# --------------------------------------------------------------------------------------


def _rich_rows():
    rows = []
    eid = 100
    for k in range(6):  # Aug 2025 .. Jan 2026 history, request 2026-02-05
        m = 8 + k
        y = 2025 + (m - 1) // 12
        m = (m - 1) % 12 + 1
        rows.append(dict(event_id=f"event_{eid}", user_id="user_A", event_type="expense", description="Rent", category="rent",
                         direction="debit", amount="300", currency="INR", event_date=f"{y}-{m:02d}-04", settlement_date=f"{y}-{m:02d}-04",
                         status="settled", linked_event_id="", flexibility="fixed", minimum_allowed_amount="")); eid += 1
        rows.append(dict(event_id=f"event_{eid}", user_id="user_A", event_type="income", description="Payroll credit", category="salary",
                         direction="credit", amount="700", currency="INR", event_date=f"{y}-{m:02d}-15", settlement_date=f"{y}-{m:02d}-15",
                         status="settled", linked_event_id="", flexibility="fixed", minimum_allowed_amount="")); eid += 1
        rows.append(dict(event_id=f"event_{eid}", user_id="user_A", event_type="subscription", description="Streaming", category="streaming",
                         direction="debit", amount="20", currency="INR", event_date=f"{y}-{m:02d}-10", settlement_date="",
                         status="settled", linked_event_id="", flexibility="reducible_or_stoppable", minimum_allowed_amount="8")); eid += 1
    d = date(2025, 11, 5)
    while d < date(2026, 2, 5):  # weekly groceries
        rows.append(dict(event_id=f"event_{eid}", user_id="user_A", event_type="expense", description=f"Groceries {eid}", category="groceries",
                         direction="debit", amount="50", currency="INR", event_date=d.isoformat(), settlement_date=d.isoformat(),
                         status="settled", linked_event_id="", flexibility="fixed", minimum_allowed_amount="")); eid += 1
        d += timedelta(days=7)
    # future rows
    rows.append(dict(event_id="event_pend", user_id="user_A", event_type="expense", description="Pending fuel", category="transport",
                     direction="debit", amount="30", currency="INR", event_date="2026-02-06", settlement_date="2026-02-06",
                     status="pending", linked_event_id="", flexibility="fixed", minimum_allowed_amount=""))
    rows.append(dict(event_id="event_refund", user_id="user_A", event_type="refund", description="Refund", category="shopping",
                     direction="credit", amount="500", currency="INR", event_date="2026-02-07", settlement_date="2026-02-07",
                     status="pending", linked_event_id="", flexibility="fixed", minimum_allowed_amount=""))
    rows.append(dict(event_id="event_fail", user_id="user_A", event_type="expense", description="Failed", category="utilities",
                     direction="debit", amount="999", currency="INR", event_date="2026-02-08", settlement_date="2026-02-08",
                     status="failed", linked_event_id="", flexibility="fixed", minimum_allowed_amount=""))
    rows.append(dict(event_id="event_val", user_id="user_A", event_type="investment_valuation", description="Fund", category="investment",
                     direction="non_cash", amount="9999", currency="INR", event_date="2026-02-01", settlement_date="2026-02-01",
                     status="unrealized", linked_event_id="", flexibility="fixed", minimum_allowed_amount=""))
    rows += [dict(r) for r in SYNTHETIC["financial_events.csv"] if r["user_id"] == "user_B"]
    return rows


@pytest.fixture
def rich(tmp_path: Path):
    profiles = [dict(p) for p in SYNTHETIC["financial_profiles.csv"]]
    profiles[0].update(current_available_balance="1000", minimum_balance_to_keep="200",
                       expense_categories_user_is_willing_to_stop="streaming", expense_categories_user_is_willing_to_reduce="streaming",
                       payment_methods_user_will_consider="full_payment|partial_payment|installments", max_installment_months="3")
    reqs = [dict(r) for r in SYNTHETIC["requests.csv"]]
    reqs[0].update(requested_amount="400", desired_completion_date="2026-04-05")
    images = [{"image_id": "image_1", "user_id": "user_B", "request_id": "request_B", "related_event_id": "event_5"}]
    d = write_synthetic(tmp_path, {"financial_events.csv": _rich_rows(), "financial_profiles.csv": profiles, "requests.csv": reqs, "images.csv": images})
    ds = load_dataset(d)
    return ds, build_context(ds, ds.request_by_id["request_A"])


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def test_reconcile_base_classes(rich):
    ds, ctx = rich
    led = reconcile(ctx, [])
    by = led.by_id
    assert by["event_pend"].cash_class == RESERVED
    assert by["event_refund"].cash_class == EXCLUDED  # pending credit
    assert by["event_fail"].cash_class == EXCLUDED
    assert by["event_val"].cash_class == EXCLUDED
    assert by["event_100"].cash_class == HISTORY


def test_reconcile_applies_evidence(rich):
    ds, ctx = rich
    led = reconcile(ctx, [_ev("message_1", _item("cancellation", "event_pend"), _item("settlement", "event_refund", new_date=date(2026, 2, 9)),
                                _item("income_change", amount=800, currency="INR", eff=date(2026, 3, 1)))])
    by = led.by_id
    assert by["event_pend"].cash_class == EXCLUDED and "cancellation" in by["event_pend"].edits
    assert by["event_refund"].cash_class == CONFIRMED and by["event_refund"].effective_date == date(2026, 2, 9)
    assert len(led.income_overrides) == 1 and led.income_overrides[0].new_amount == D(800)
    assert "evidence:message_1" in led.evidence_used


def test_reconcile_unresolved_and_settled_protection(rich):
    ds, ctx = rich
    led = reconcile(ctx, [_ev("message_1", _item("unresolved", "event_pend", reason="confidence low"), _item("cancellation", "event_100"))])
    assert led.by_id["event_pend"].cash_class == RESERVED  # unresolved => no edit
    assert led.by_id["event_100"].cash_class == HISTORY  # settled row never cancelled
    assert any("ignored on a settled row" in n for n in led.notes)


def test_blank_amount_safer_estimate(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctx = build_context(ds, ds.request_by_id["request_A"])
    led = reconcile(ctx, [])  # event_3 blank, no evidence
    r = led.by_id["event_3"]
    assert "estimated_amount" in r.flags and r.home_amount is not None and r.home_amount > 0
    led2 = reconcile(ctx, [_ev("image_1", _item("amount_extraction", "event_3", amount=42, currency="INR", src="image_1"))])
    assert led2.by_id["event_3"].home_amount == D(42) and "amount_from_image" in led2.by_id["event_3"].flags


# --------------------------------------------------------------------------------------
# Recurrence and state
# --------------------------------------------------------------------------------------


def test_recurrence_detects_monthly_weekly_and_projects(rich):
    ds, ctx = rich
    led = reconcile(ctx, [])
    pats = {p.category: p for p in detect_patterns(led)}
    assert pats["rent"].cadence == "monthly" and pats["rent"].projected[0] == date(2026, 3, 4)
    assert pats["salary"].cadence == "monthly" and pats["salary"].is_income
    assert pats["groceries"].cadence == "weekly" and len(pats["groceries"].projected) >= 12
    assert "investment" not in pats and "shopping" not in pats


def test_state_flows_and_flexible_catalogue(rich):
    ds, ctx = rich
    st = build_state(reconcile(ctx, []))
    kinds = {f.kind for f in st.flows}
    assert {"reserved", "projected_income", "projected_expense"} <= kinds
    assert not any(f.label == "event_refund" for f in st.flows)
    (flex,) = st.flexible
    assert flex.category == "streaming" and flex.can_stop and flex.can_reduce and flex.floor == D(8)


def test_income_end_stops_projection(rich):
    ds, ctx = rich
    led = reconcile(ctx, [_ev("message_1", _item("income_end", eff=date(2026, 3, 1)))])
    st = build_state(led)
    inc = [f for f in st.flows if f.kind == "projected_income"]
    assert inc and all(f.on < date(2026, 3, 1) for f in inc)


# --------------------------------------------------------------------------------------
# Forecast closed forms
# --------------------------------------------------------------------------------------


def test_path_headroom_safe_and_earliest():
    start = date(2026, 1, 1)
    bal = [D(1000), D(900), D(950), D(1200), D(1100)]
    p = BalancePath(start, D(200), bal)
    assert p.headroom(start) == D(700)  # min 900 - 200
    assert p.headroom(start + timedelta(days=3)) == D(900)
    assert safe_amount(p, D(5000)) == D(700) and safe_amount(p, D(100)) == D(100)
    assert earliest_full_payment_date(p, D(800)) == start + timedelta(days=3)
    assert earliest_full_payment_date(p, D(10000)) is None
    assert p.is_safe([Payment(start, D(700))]) and not p.is_safe([Payment(start, D(701))])
    assert p.first_breach([Payment(start, D(701))]) == start + timedelta(days=1)


def test_closed_form_matches_brute_force():
    import random

    rnd = random.Random(7)
    start = date(2026, 1, 1)
    for _ in range(30):
        bal = [D(rnd.randint(0, 2000)) for _ in range(91)]
        p = BalancePath(start, D(300), bal)
        req = D(rnd.randint(1, 1500))
        brute = next((start + timedelta(days=i) for i in range(91) if p.is_safe([Payment(start + timedelta(days=i), req)], from_date=start)), None)
        assert earliest_full_payment_date(p, req) == brute
        s = safe_amount(p, req)
        if p.headroom(start) >= 0:  # a path that already breaches the minimum has no safe payment at all
            assert p.is_safe([Payment(start, s)]) and (s == req or not p.is_safe([Payment(start, s + D("0.01"))]))
        else:
            assert s == 0


# --------------------------------------------------------------------------------------
# Plans, gates, policy
# --------------------------------------------------------------------------------------


def test_plans_generation_and_gates(rich):
    ds, ctx = rich
    st = build_state(reconcile(ctx, []))
    path = build_path(st)
    req = ctx.request
    safe = safe_amount(path, req.requested_amount)
    earliest = earliest_full_payment_date(path, req.requested_amount)
    plans = generate_plans(ctx, safe, earliest)
    kinds = [p.kind for p in plans]
    assert kinds[0] == FULL_NOW and INSTALLMENTS in kinds
    inst = next(p for p in plans if p.kind == INSTALLMENTS)
    assert inst.total_paid == D(420) and inst.n_payments == 3
    ev = evaluate_plan(inst, ctx, path)
    assert ev.eligible or ev.reason  # evaluated either way
    # preference gate
    prof = ctx.profile.model_copy(update={"payment_methods_user_will_consider": ()})
    ctx2 = ctx.__class__(**{**ctx.__dict__, "profile": prof.model_copy(update={"payment_methods_user_will_consider": ("installments",)})})
    assert not evaluate_plan(plans[0], ctx2, path).eligible
    # deadline gate
    late = ctx.__class__(**{**ctx.__dict__, "request": req.model_copy(update={"desired_completion_date": req.request_date})})
    assert "deadline" in (evaluate_plan(inst, late, path).reason or "")


def test_decide_prefers_cheaper_plan_and_maps_status(rich):
    ds, ctx = rich
    st = build_state(reconcile(ctx, []))
    path = build_path(st)
    req = ctx.request
    safe = safe_amount(path, req.requested_amount)
    earliest = earliest_full_payment_date(path, req.requested_amount)
    d = decide(ctx, st, path, safe, earliest)
    assert d.status.value in {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
    if d.plan and d.plan.kind == FULL_NOW and not d.plan.changes:
        assert d.status.value == "affordable_now" and d.earliest_date == req.request_date
    assert all(e.plan.kind for e in d.evaluated)


def test_spending_change_rescues_unsafe_full_payment(rich):
    ds, ctx = rich
    st = build_state(reconcile(ctx, []))
    path = build_path(st)
    req = ctx.request
    # make the full payment just unsafe: request = headroom + 15 (streaming stop saves 20/month)
    h = path.headroom(req.request_date)
    # partial would win (no changes needed), so disallow it on the request to exercise the change search
    # ...and set the deadline before the next salary so `wait` cannot win either (the request_06 situation)
    ctx2 = ctx.__class__(**{**ctx.__dict__, "request": req.model_copy(update={"requested_amount": h + D(15), "allows_partial_payment": False,
                                                                              "desired_completion_date": req.request_date + timedelta(days=5)})})
    d = decide(ctx2, st, path, safe_amount(path, h + D(15)), earliest_full_payment_date(path, h + D(15)))
    assert d.plan is not None and d.plan.kind == FULL_NOW and d.plan.changes
    assert d.status.value == "affordable_with_plan" and d.changes[0].action == "stop"


# --------------------------------------------------------------------------------------
# Consistency validator
# --------------------------------------------------------------------------------------


def _decision(rich):
    ds, ctx = rich
    st = build_state(reconcile(ctx, []))
    path = build_path(st)
    req = ctx.request
    safe = safe_amount(path, req.requested_amount)
    d = decide(ctx, st, path, safe, earliest_full_payment_date(path, req.requested_amount))
    d._ctx = ctx
    fs = build_fact_sheet(ctx, d, st, {}, [])
    return ctx, st, path, d, fs


def test_validator_accepts_good_row_and_rejects_contradictions(rich):
    ctx, st, path, d, fs = _decision(rich)
    row = row_from_decision(d, template_explanation(d, ctx))
    assert validate_row(row, ctx, d, st, path, fs.numbers) == []
    bad = OutputRow(**{**row.__dict__, "affordability_status": "affordable_now", "earliest_date_for_full_payment": "2026-03-01"})
    errs = validate_row(bad, ctx, d, st, path, fs.numbers)
    assert any("earliest" in e for e in errs)
    bad2 = OutputRow(**{**row.__dict__, "recommended_payment_method": "installments", "affordability_status": "affordable_with_plan",
                        "payment_plan": "2026-02-05:100|2026-03-05:100"})
    assert any("does not match a supplied option" in e for e in validate_row(bad2, ctx, d, st, path, fs.numbers))
    bad3 = OutputRow(**{**row.__dict__, "spending_changes_needed": "stop:event_1|stop:event_2|stop:event_3|stop:event_4"})
    assert any("more than three" in e for e in validate_row(bad3, ctx, d, st, path, fs.numbers))
    bad4 = OutputRow(**{**row.__dict__, "decision_explanation": "Pay INR 123456789 today."})
    assert any("unknown number" in e for e in validate_row(bad4, ctx, d, st, path, fs.numbers))


def test_finalize_repairs_explanation_then_falls_back(rich):
    ctx, st, path, d, fs = _decision(rich)
    row = row_from_decision(d, "Pay INR 123456789 today.")
    fixed = finalize(row, ctx, d, st, path, fs.numbers, lambda dd: template_explanation(dd, ctx))
    assert fixed.repairs and "123456789" not in fixed.decision_explanation and not fixed.fallback
    broken = row_from_decision(d, template_explanation(d, ctx))
    broken.amount_safe_to_pay = "-5"
    fb = finalize(broken, ctx, d, st, path, fs.numbers, lambda dd: template_explanation(dd, ctx))
    assert fb.fallback and fb.affordability_status == "not_affordable" and fb.payment_plan == "none"


def test_formatting_rules():
    assert fmt_plan_amount(D("620.4")) == "620.40" and fmt_plan_amount(D("25256")) == "25256"
    assert fmt_safe_amount(D("603.30")) == "603.3" and fmt_safe_amount(D("17229139.20")) == "17229139.2"
    assert fmt_safe_amount(D("28820")) == "28820"


# --------------------------------------------------------------------------------------
# Pipeline: isolation, order, usage report
# --------------------------------------------------------------------------------------


def test_pipeline_no_llm_order_and_isolation(rich, monkeypatch):
    ds, ctx = rich
    ledger = UsageLedger()
    pipe = Pipeline(ds, None, None, ledger, PipelineConfig(use_llm=False, cache=None))
    # break one request deep in the pipeline
    import buyorwait.pipeline as pl

    real = pl.decide

    def boom(ctx_, *a, **k):
        if ctx_.request_id == "request_B":
            raise RuntimeError("simulated failure")
        return real(ctx_, *a, **k)

    monkeypatch.setattr(pl, "decide", boom)
    outcomes = asyncio.run(pipe.run(list(ds.requests)))
    assert [o.request_id for o in outcomes] == ["request_A", "request_B"]
    assert not outcomes[0].row.fallback
    assert outcomes[1].row.fallback and outcomes[1].stage_reached == "policy" and "simulated failure" in outcomes[1].error
    report = build_usage_report(ledger, 2, "test")
    assert "Model calls: 0" in report and "fallback: 1" in report


def test_fallback_row_shape():
    fb = fallback_row("request_X", D("12.5"), "EUR", "why")
    assert fb.as_list()[:7] == ["request_X", "12.5", "not_affordable", "not_recommended", "none", "", "none"]


def test_date_only_income_change_shifts_salary(rich):
    """A payroll notice that only moves the pay date (request_07 pattern) shifts projected salary dates."""
    from buyorwait.agents.evidence_agent import validate_item
    from buyorwait.agents.schemas import EvidenceItem

    ds, ctx = rich
    src = ctx.evidence_sources[0]
    raw = EvidenceItem(kind="income_change", target_event_id=None, new_amount=None, currency=None,
                       new_date="2026-03-23", effective_from=None, confidence="high", source_ref=src.source_id, quote="now expected on 2026-03-23")
    it = validate_item(raw, ctx, src)
    assert it.usable and it.kind == "income_change" and it.new_date == date(2026, 3, 23)
    led = reconcile(ctx, [ValidatedEvidence(source_id=src.source_id, items=(it,), injection_detected=False, summary="")])
    assert led.income_overrides and led.income_overrides[0].new_day == 23 and not led.income_overrides[0].ends
    st = build_state(led)
    inc = sorted(f.on for f in st.flows if f.kind == "projected_income")
    assert date(2026, 2, 15) in inc  # before the change: unchanged
    assert all(d.day == 23 for d in inc if d >= date(2026, 3, 23))
    assert st.flows and all(f.amount > 0 for f in st.flows if f.kind == "projected_income")
