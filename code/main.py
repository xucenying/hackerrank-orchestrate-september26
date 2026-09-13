"""Buy or Wait? — entry point.

    python code/main.py                 # full run on dataset/requests.csv -> <repo>/output.csv
    python code/main.py --sample        # run on dataset/sample_requests.csv (writes code/evaluation/sample_output.csv)
    python code/main.py --no-llm        # deterministic only (no API key needed)
    python code/main.py --limit 20      # first N requests
    python code/main.py --clear-cache   # drop cached model outputs before running
    python code/main.py --concurrency 4

Secrets are read from the environment / .env (OPENAI_API_KEY, OPENAI_MODEL). See README.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from buyorwait.config import CODE_DIR, OUTPUT_CSV, load_env  # noqa: E402
from buyorwait.data import DatasetValidationError, load_dataset  # noqa: E402
from buyorwait.infra.cache import JsonCache  # noqa: E402
from buyorwait.infra.csv_writer import write_output_csv  # noqa: E402
from buyorwait.infra.usage import UsageLedger, build_usage_report  # noqa: E402
from buyorwait.pipeline import Pipeline, PipelineConfig  # noqa: E402

CACHE_DIR = CODE_DIR / ".cache"
RUNS_DIR = CODE_DIR / ".runs"
USAGE_REPORT = CODE_DIR / "evaluation" / "usage_report.md"


def build_pipeline(ds, *, use_llm: bool, concurrency: int, cache: JsonCache | None, ledger: UsageLedger,
                   evidence_batch: int = 4, explanation_batch: int = 8) -> Pipeline:
    extractor = explainer = None
    if use_llm:
        from buyorwait.agents.evidence_agent import PicoEvidenceExtractor
        from buyorwait.agents.explanation_agent import PicoExplainer
        from buyorwait.config import make_evidence_client, make_explanation_client

        extractor = PicoEvidenceExtractor(make_evidence_client())
        explainer = PicoExplainer(make_explanation_client())
    cfg = PipelineConfig(max_concurrency=concurrency, use_llm=use_llm, cache=cache,
                         evidence_batch_size=evidence_batch, explanation_batch_size=explanation_batch)
    return Pipeline(ds, extractor, explainer, ledger, cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Buy or Wait? affordability agent")
    ap.add_argument("--sample", action="store_true", help="run on sample_requests.csv instead of requests.csv")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-llm", action="store_true", help="deterministic only; template explanations; no API calls")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--evidence-batch", type=int, default=4, help="message-only evidence sources per model call (1 = no batching)")
    ap.add_argument("--explanation-batch", type=int, default=8, help="fact sheets per explanation call (1 = no batching)")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--usage-report", type=Path, default=USAGE_REPORT)
    args = ap.parse_args(argv)

    load_env()
    t0 = time.time()
    try:
        ds = load_dataset(strict=True)
    except DatasetValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    requests = list(ds.sample_requests if args.sample else ds.requests)
    if args.limit:
        requests = requests[: args.limit]

    cache = None if args.no_cache else JsonCache(CACHE_DIR)
    if cache and args.clear_cache:
        print(f"cleared {cache.clear()} cached outputs")
    ledger = UsageLedger()
    pipe = build_pipeline(ds, use_llm=not args.no_llm, concurrency=args.concurrency, cache=cache, ledger=ledger,
                          evidence_batch=args.evidence_batch, explanation_batch=args.explanation_batch)

    print(f"processing {len(requests)} request(s) | llm={'on' if not args.no_llm else 'off'} | concurrency={args.concurrency} | batches: evidence {args.evidence_batch}, explanation {args.explanation_batch}")
    outcomes = asyncio.run(pipe.run(requests))

    out_path = args.output or (CODE_DIR / "evaluation" / "sample_output.csv" if args.sample else OUTPUT_CSV)
    n = write_output_csv((o.row for o in outcomes), out_path)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger.write_jsonl(RUNS_DIR / run_id / "calls.jsonl")
    label = f"{'sample' if args.sample else 'full'} run {run_id} ({'no-llm' if args.no_llm else 'llm'})"
    report = build_usage_report(ledger, len(requests), label, {"hits": cache.hits, "misses": cache.misses} if cache else None)
    if not args.sample or args.usage_report != USAGE_REPORT:
        args.usage_report.parent.mkdir(parents=True, exist_ok=True)
        args.usage_report.write_text(report, encoding="utf-8")
    else:
        (RUNS_DIR / run_id / "usage_report.md").write_text(report, encoding="utf-8")

    fallbacks = sum(1 for o in outcomes if o.row.fallback)
    repairs = sum(1 for o in outcomes if o.row.repairs and not o.row.fallback)
    errors = [o for o in outcomes if o.error]
    print(f"wrote {n} rows -> {out_path}")
    print(f"fallback rows: {fallbacks} | repaired rows: {repairs} | errors: {len(errors)} | {time.time() - t0:.1f}s")
    for o in errors[:10]:
        print(f"  {o.request_id} failed at {o.stage_reached}: {o.error}")
    print(f"run log: {RUNS_DIR / run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
