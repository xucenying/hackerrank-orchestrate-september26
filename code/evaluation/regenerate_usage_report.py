"""Rebuild evaluation/usage_report.md from a recorded run directory (code/.runs/<timestamp>).

    python code/evaluation/regenerate_usage_report.py code/.runs/20260913T050000Z [--out path]

Useful after setting OPENAI_PRICE_INPUT_PER_1M / OPENAI_PRICE_OUTPUT_PER_1M so cost columns can be
filled without re-running the model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buyorwait.config import CODE_DIR, load_env  # noqa: E402
from buyorwait.infra.usage import CallRecord, UsageLedger, build_usage_report  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, default=CODE_DIR / "evaluation" / "usage_report.md")
    ap.add_argument("--requests", type=int, default=None, help="number of requests in the run (default: from requests.json)")
    args = ap.parse_args(argv)
    load_env()
    ledger = UsageLedger()
    for line in (args.run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            ledger.record(CallRecord(**json.loads(line)))
    rj = args.run_dir / "requests.json"
    if rj.exists():
        ledger.request_stages = json.loads(rj.read_text(encoding="utf-8"))
    n = args.requests or len(ledger.request_stages)
    report = build_usage_report(ledger, n, f"regenerated from {args.run_dir.name}")
    args.out.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
