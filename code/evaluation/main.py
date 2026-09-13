"""Development evaluation: run the pipeline on dataset/sample_requests.csv and compare each output
field with the organizer-provided answers.

    python code/evaluation/main.py            # with LLM stages (uses cache)
    python code/evaluation/main.py --no-llm   # deterministic only

Writes code/evaluation/sample_output.csv and code/evaluation/sample_evaluation.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buyorwait.config import CODE_DIR, load_env  # noqa: E402
from buyorwait.data import load_dataset  # noqa: E402
from buyorwait.infra.cache import JsonCache  # noqa: E402
from buyorwait.infra.csv_writer import write_output_csv  # noqa: E402
from buyorwait.infra.usage import UsageLedger  # noqa: E402
from main import CACHE_DIR, build_pipeline  # noqa: E402

FIELDS = ["amount_safe_to_pay", "affordability_status", "recommended_payment_method", "payment_plan",
          "earliest_date_for_full_payment", "spending_changes_needed"]


def _close(a: str, b: str, rel=Decimal("0.005")) -> bool:
    try:
        x, y = Decimal(a), Decimal(b)
    except Exception:
        return a == b
    return abs(x - y) <= max(Decimal("0.01"), abs(y) * rel)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args(argv)
    load_env()
    ds = load_dataset()
    ledger = UsageLedger()
    cache = None if args.no_llm else JsonCache(CACHE_DIR)
    pipe = build_pipeline(ds, use_llm=not args.no_llm, concurrency=args.concurrency, cache=cache, ledger=ledger)
    outcomes = asyncio.run(pipe.run(list(ds.sample_requests)))
    out_dir = CODE_DIR / "evaluation"
    write_output_csv((o.row for o in outcomes), out_dir / "sample_output.csv")

    exact = {f: 0 for f in FIELDS}
    near = 0
    lines = []
    for o, s in zip(outcomes, ds.sample_requests):
        r = o.row
        gt = {
            "amount_safe_to_pay": str(s.amount_safe_to_pay.normalize()) if s.amount_safe_to_pay != s.amount_safe_to_pay.to_integral_value() else str(int(s.amount_safe_to_pay)),
            "affordability_status": s.affordability_status.value,
            "recommended_payment_method": s.recommended_payment_method.value,
            "payment_plan": s.payment_plan,
            "earliest_date_for_full_payment": s.earliest_date_for_full_payment.isoformat() if s.earliest_date_for_full_payment else "",
            "spending_changes_needed": s.spending_changes_needed,
        }
        ours = {f: getattr(r, f) for f in FIELDS}
        marks = []
        for f in FIELDS:
            ok = _close(ours[f], gt[f]) if f == "amount_safe_to_pay" else (ours[f] == gt[f])
            exact[f] += ok
            if not ok:
                marks.append(f"{f}: ours={ours[f]!r} gt={gt[f]!r}")
        if _close(ours["amount_safe_to_pay"], gt["amount_safe_to_pay"], Decimal("0.10")):
            near += 1
        lines.append(f"### {s.request_id}  {'OK' if not marks else 'MISS'}\n" + ("\n".join(f"- {m}" for m in marks) or "- all fields match") +
                     f"\n- explanation (ours): {r.decision_explanation}\n- explanation (gt): {s.decision_explanation}\n" +
                     (f"- repairs: {r.repairs}\n" if r.repairs else ""))
    n = len(outcomes)
    summary = ["# Sample evaluation", "", f"Mode: {'no-llm' if args.no_llm else 'llm'} | requests: {n}", "",
               "| Field | Exact matches |", "|---|---|"] + [f"| {f} | {exact[f]}/{n} |" for f in FIELDS] + \
              [f"| amount_safe_to_pay within 10% | {near}/{n} |", "",
               f"Rows with status AND method correct: {sum(1 for o, s in zip(outcomes, ds.sample_requests) if o.row.affordability_status == s.affordability_status.value and o.row.recommended_payment_method == s.recommended_payment_method.value)}/{n}",
               f"Fallback rows: {sum(1 for o in outcomes if o.row.fallback)} | repaired rows: {sum(1 for o in outcomes if o.row.repairs)}", ""]
    report = "\n".join(summary + lines)
    (out_dir / "sample_evaluation.md").write_text(report, encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
