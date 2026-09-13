"""Model-call ledger and usage report (CLAUDE.md §25-§26). Never fabricates numbers: every
figure comes from recorded calls, and cost is reported only when a price is configured."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class CallRecord:
    request_id: str
    agent: str
    model: Optional[str]
    provider: str
    llm_calls: int
    tokens_input: int
    tokens_output: int
    duration_ms: int
    cached: bool
    error: Optional[str]
    retries: int = 0
    fallback: bool = False
    batch_size: int = 1  # >1 when one call served several sources / fact sheets
    ts: float = field(default_factory=time.time)


@dataclass
class UsageLedger:
    records: list[CallRecord] = field(default_factory=list)
    request_stages: dict[str, dict] = field(default_factory=dict)

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)

    def note_request(self, request_id: str, **info) -> None:
        self.request_stages.setdefault(request_id, {}).update(info)

    def write_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for r in self.records:
                fh.write(json.dumps(asdict(r)) + "\n")
        (path.parent / "requests.json").write_text(json.dumps(self.request_stages, indent=1, default=str), encoding="utf-8")


def _price_env(model: str) -> tuple[Optional[float], Optional[float], str]:
    """USD per 1M tokens from env (OPENAI_PRICE_INPUT_PER_1M / OPENAI_PRICE_OUTPUT_PER_1M).
    Returns (in, out, source)."""
    pi, po = os.environ.get("OPENAI_PRICE_INPUT_PER_1M"), os.environ.get("OPENAI_PRICE_OUTPUT_PER_1M")
    src = os.environ.get("OPENAI_PRICE_SOURCE") or "environment (OPENAI_PRICE_*_PER_1M)"
    try:
        if pi and po:
            return float(pi), float(po), f"USD {float(pi):g} in / {float(po):g} out per 1M tokens; {src}"
    except ValueError:
        pass
    return None, None, "not configured"


def build_usage_report(ledger: UsageLedger, n_requests: int, run_label: str, cache_stats: Optional[dict] = None) -> str:
    calls = [r for r in ledger.records if not r.cached and r.error is None and r.llm_calls > 0]
    cached = [r for r in ledger.records if r.cached]
    failed = [r for r in ledger.records if r.error]
    by_model: dict[tuple[str, str], dict] = {}
    for r in calls:
        k = (r.provider, r.model or "unknown")
        d = by_model.setdefault(k, {"calls": 0, "in": 0, "out": 0, "ms": 0, "agents": set()})
        d["calls"] += r.llm_calls
        d["in"] += r.tokens_input
        d["out"] += r.tokens_output
        d["ms"] += r.duration_ms
        d["agents"].add(r.agent)
    tot_calls = sum(d["calls"] for d in by_model.values())
    tot_in = sum(d["in"] for d in by_model.values())
    tot_out = sum(d["out"] for d in by_model.values())
    tot = tot_in + tot_out
    lines = [
        "# Usage Report",
        "",
        f"Run: {run_label}  ",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}  ",
        f"Requests processed: {n_requests}",
        "",
        "## Per-model totals",
        "",
        "| Provider | Model | Agents | Model calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    total_cost: Optional[float] = 0.0
    price_src = "not configured"
    for (prov, model), d in sorted(by_model.items()):
        pi, po, price_src = _price_env(model)
        if pi is not None and po is not None:
            cost = d["in"] / 1e6 * pi + d["out"] / 1e6 * po
            cost_s = f"{cost:.4f}"
            total_cost = (total_cost or 0.0) + cost
        else:
            cost_s = "n/a"
            total_cost = None
        lines.append(f"| {prov} | {model} | {', '.join(sorted(d['agents']))} | {d['calls']} | {d['in']:,} | {d['out']:,} | {d['in'] + d['out']:,} | {cost_s} |")
    if not by_model:
        lines.append("| — | — | — | 0 | 0 | 0 | 0 | 0 |")
    avg = lambda x: (x / n_requests) if n_requests else 0
    lines += [
        "",
        "## Overall",
        "",
        f"- Model calls: {tot_calls}",
        f"- Input tokens: {tot_in:,}",
        f"- Output tokens: {tot_out:,}",
        f"- Total tokens: {tot:,}",
        f"- Average calls per request: {avg(tot_calls):.3f}",
        f"- Average tokens per request: {avg(tot):.1f}",
        f"- Estimated total cost: {'USD ' + format(total_cost, '.4f') if total_cost is not None else 'n/a (price ' + price_src + ')'}",
        f"- Estimated cost per request: {'USD ' + format(avg(total_cost), '.6f') if total_cost is not None else 'n/a'}",
        f"- Pricing source: {price_src}",
        "",
        "## Reliability",
        "",
        f"- Cached model outputs reused: {len(cached)}",
        f"- Failed model calls (after retries): {len(failed)}",
        f"- Retries performed: {sum(r.retries for r in ledger.records)}",
        f"- Batched calls: {sum(1 for r in ledger.records if r.batch_size > 1 and not r.cached and r.error is None)} "
        f"(covering {sum(r.batch_size for r in ledger.records if r.batch_size > 1 and not r.cached and r.error is None)} items)",
        f"- Requests that used a fallback: {sum(1 for v in ledger.request_stages.values() if v.get('fallback'))}",
        f"- Requests with validator repairs: {sum(1 for v in ledger.request_stages.values() if v.get('repairs'))}",
    ]
    if cache_stats:
        lines.append(f"- Cache hits / misses this run: {cache_stats.get('hits', 0)} / {cache_stats.get('misses', 0)}")
    lines += [
        "",
        "Notes: token counts come from the provider's usage fields via PicoAgents `Usage`. Requests",
        "without messages or images make no evidence call. Cost is computed only when",
        "`OPENAI_PRICE_INPUT_PER_1M` and `OPENAI_PRICE_OUTPUT_PER_1M` are set; no price is assumed.",
        "No API keys or credentials are included.",
    ]
    return "\n".join(lines) + "\n"
