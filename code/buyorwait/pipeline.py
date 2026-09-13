"""End-to-end pipeline and batch runner.

    contexts -> evidence (LLM #1, batched, optional) -> reconciliation -> state -> forecast ->
    plans -> policy -> explanation (LLM #2, batched, optional) -> consistency -> OutputRow

Guarantees:

* Every request is isolated: an exception in any deterministic stage yields the safe fallback
  row for that request only, with the stage and error recorded.
* Model calls are batched where safe (message-only evidence sources; all explanations) and
  otherwise made singly (images). A batch that fails or omits an item falls back to single calls
  for the affected items, so batching never changes which requests get an answer.
* Cache lookups are per source / per request, before batching; only misses reach the model.
* Bounded concurrency: all model calls run under one semaphore.
* Output order equals input order regardless of completion order.
* Retries: transient model errors are retried with bounded exponential backoff.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Optional, Sequence

from .agents.evidence_agent import (
    PROMPT_VERSION as EV_VERSION,
    EvidenceExtractor,
    EvidenceResult,
    NullEvidenceExtractor,
    facts_block,
    untrusted_block,
    validate_evidence,
)
from .agents.explanation_agent import (
    PROMPT_VERSION as EX_VERSION,
    Explainer,
    ExplanationResult,
    FactSheet,
    TemplateExplainer,
    build_fact_sheet,
    template_explanation,
)
from .agents.schemas import StructuredEvidence, ValidatedEvidence
from .data.context import EvidenceSource, RequestContext, build_context
from .data.loader import Dataset
from .data.models import Request
from .finance.forecast import build_path, earliest_full_payment_date, safe_amount
from .finance.reconciliation import reconcile
from .finance.state import build_state
from .infra.cache import JsonCache
from .infra.usage import CallRecord, UsageLedger
from .policy.consistency import OutputRow, fallback_row, finalize, row_from_decision
from .policy.decision_policy import decide

TRANSIENT_MARKERS = ("RateLimit", "Timeout", "timeout", "Connection", "503", "502", "500", "overloaded", "APIConnection")


@dataclass
class PipelineConfig:
    max_concurrency: int = 8
    max_retries: int = 3
    backoff_base_s: float = 1.0
    use_llm: bool = True
    cache: Optional[JsonCache] = None
    provider: str = "openai"
    evidence_batch_size: int = 4  # message-only sources per evidence call (1 = no batching)
    explanation_batch_size: int = 8  # fact sheets per explanation call (1 = no batching)


@dataclass
class RequestOutcome:
    row: OutputRow
    request_id: str
    stage_reached: str
    evidence: list[ValidatedEvidence] = field(default_factory=list)
    decision_notes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_ms: int = 0


@dataclass
class _Work:
    """Per-request state carried between phases."""

    request: Request
    ctx: Optional[RequestContext] = None
    evidence: list[ValidatedEvidence] = field(default_factory=list)
    stage: str = "context"
    error: Optional[str] = None
    safe: Optional[Decimal] = None
    decision: object = None
    state: object = None
    base_path: object = None
    fs: Optional[FactSheet] = None
    template: str = ""
    descriptions: dict = field(default_factory=dict)
    ledger_notes: list[str] = field(default_factory=list)
    evidence_used: list[str] = field(default_factory=list)
    explanation: Optional[ExplanationResult] = None
    t0: float = field(default_factory=time.time)


class Pipeline:
    def __init__(self, ds: Dataset, extractor: Optional[EvidenceExtractor], explainer: Optional[Explainer],
                 ledger: UsageLedger, config: PipelineConfig):
        self.ds = ds
        self.extractor = extractor or NullEvidenceExtractor()
        self.explainer = explainer or TemplateExplainer()
        self.ledger = ledger
        self.cfg = config
        self.sem = asyncio.Semaphore(config.max_concurrency)

    # ------------------------------------------------------------------ retry helper
    async def _retry(self, fn, *args, request_id: str, agent: str):
        attempt = 0
        while True:
            res = await fn(*args)
            err = getattr(getattr(res, "evidence", res), "error", None) if hasattr(res, "evidence") else getattr(res, "error", None)
            transient = err is not None and any(m in err for m in TRANSIENT_MARKERS)
            if err is None or not transient or attempt >= self.cfg.max_retries:
                return res, attempt
            attempt += 1
            delay = self.cfg.backoff_base_s * (2 ** (attempt - 1)) * (0.8 + 0.4 * random.random())
            self.ledger.note_request(request_id, **{f"retry_{agent}_{attempt}": err})
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ evidence
    def _evidence_key(self, ctx: RequestContext, src: EvidenceSource) -> Optional[str]:
        if self.cfg.cache is None or not self.cfg.use_llm:
            return None
        model = getattr(self.extractor, "model_name", None)
        img = Path(src.image_path).read_bytes() if src.image_path else b""
        return JsonCache.key(EV_VERSION, "StructuredEvidence-v1", model, facts_block(ctx, src), untrusted_block(src), img)

    async def _evidence_single(self, ctx: RequestContext, src: EvidenceSource, key: Optional[str]) -> ValidatedEvidence:
        async with self.sem:
            res, retries = await self._retry(self.extractor.extract, ctx, src, request_id=ctx.request_id, agent="evidence")
        ev = res.evidence
        self.ledger.record(CallRecord(ctx.request_id, "evidence_agent", res.model, self.cfg.provider, res.llm_calls, res.tokens_input,
                                      res.tokens_output, res.duration_ms, False, ev.error, retries=retries, fallback=ev.error is not None))
        if key is not None and ev.error is None and ev.raw is not None:
            self.cfg.cache.put(key, ev.raw.model_dump(mode="json"))
        return ev

    async def _evidence_batch(self, pairs: list[tuple[RequestContext, EvidenceSource, Optional[str]]]) -> dict[str, ValidatedEvidence]:
        """One batched call; anything missing afterwards is retried singly."""
        out: dict[str, ValidatedEvidence] = {}
        if len(pairs) > 1 and hasattr(self.extractor, "extract_batch"):
            async with self.sem:
                results, usage = await self.extractor.extract_batch([(c, s) for c, s, _ in pairs])
            self.ledger.record(CallRecord("batch:" + ",".join(s.source_id for _, s, _ in pairs), "evidence_agent", usage.model, self.cfg.provider,
                                          usage.llm_calls, usage.tokens_input, usage.tokens_output, usage.duration_ms, False, usage.evidence.error,
                                          batch_size=len(pairs)))
            for ctx, src, key in pairs:
                ev = results.get(src.source_id)
                if ev is not None and ev.evidence.error is None:
                    out[src.source_id] = ev.evidence
                    if key is not None and ev.evidence.raw is not None:
                        self.cfg.cache.put(key, ev.evidence.raw.model_dump(mode="json"))
        for ctx, src, key in pairs:
            if src.source_id not in out:
                self.ledger.note_request(ctx.request_id, batch_fallback_single=src.source_id)
                out[src.source_id] = await self._evidence_single(ctx, src, key)
        return out

    async def _evidence_phase(self, works: list[_Work]) -> None:
        if not self.cfg.use_llm:
            for w in works:
                if w.ctx is not None:
                    w.evidence = [ValidatedEvidence(s.source_id, (), False, "", skipped=True) for s in w.ctx.evidence_sources]
            return
        singles: list[tuple[_Work, RequestContext, EvidenceSource, Optional[str]]] = []
        batchable: list[tuple[_Work, RequestContext, EvidenceSource, Optional[str]]] = []
        slots: dict[str, ValidatedEvidence] = {}
        model = getattr(self.extractor, "model_name", None)
        for w in works:
            if w.ctx is None:
                continue
            for src in w.ctx.evidence_sources:
                key = self._evidence_key(w.ctx, src)
                hit = self.cfg.cache.get(key) if key else None
                if hit is not None:
                    slots[src.source_id] = validate_evidence(StructuredEvidence.model_validate(hit), w.ctx, src)
                    self.ledger.record(CallRecord(w.ctx.request_id, "evidence_agent", model, self.cfg.provider, 0, 0, 0, 0, True, None))
                elif src.kind == "message" and self.cfg.evidence_batch_size > 1:
                    batchable.append((w, w.ctx, src, key))
                else:
                    singles.append((w, w.ctx, src, key))

        tasks = []
        for _, ctx, src, key in singles:
            tasks.append(self._evidence_single(ctx, src, key))
        n = self.cfg.evidence_batch_size
        batches = [batchable[i:i + n] for i in range(0, len(batchable), n)]
        for b in batches:
            tasks.append(self._evidence_batch([(c, s, k) for _, c, s, k in b]))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (w, ctx, src, key), res in zip(singles, results[: len(singles)]):
            slots[src.source_id] = res if isinstance(res, ValidatedEvidence) else ValidatedEvidence(src.source_id, (), False, "", error=str(res))
        for b, res in zip(batches, results[len(singles):]):
            if isinstance(res, dict):
                slots.update(res)
            else:
                for _, ctx, src, key in b:
                    slots[src.source_id] = ValidatedEvidence(src.source_id, (), False, "", error=str(res))
        for w in works:
            if w.ctx is not None:
                w.evidence = [slots[s.source_id] for s in w.ctx.evidence_sources if s.source_id in slots]

    # ------------------------------------------------------------------ deterministic stages
    def _deterministic(self, w: _Work) -> None:
        req = w.request
        try:
            w.stage = "reconciliation"
            ledger = reconcile(w.ctx, w.evidence)
            w.stage = "state"
            state = build_state(ledger)
            w.stage = "forecast"
            base_path = build_path(state)
            safe = safe_amount(base_path, req.requested_amount).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            w.safe = safe
            earliest = earliest_full_payment_date(base_path, req.requested_amount)
            w.stage = "policy"
            decision = decide(w.ctx, state, base_path, safe, earliest)
            decision._ctx = w.ctx
            w.descriptions = {p.event_id: p.description for p in state.patterns}
            w.stage = "explanation"
            w.fs = build_fact_sheet(w.ctx, decision, state, w.descriptions, ledger.evidence_used)
            w.template = template_explanation(decision, w.ctx, w.descriptions)
            w.decision, w.state, w.base_path = decision, state, base_path
            w.ledger_notes, w.evidence_used = ledger.notes[:10] + decision.notes[:3], ledger.evidence_used
        except Exception as exc:
            w.error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ explanations
    def _explanation_key(self, fs: FactSheet, template: str) -> Optional[str]:
        model = getattr(self.explainer, "model_name", None)
        if self.cfg.cache is None or not self.cfg.use_llm or not model:
            return None
        return JsonCache.key(EX_VERSION, "Explanation-v1", model, fs.to_json(), template)

    async def _explain_single(self, w: _Work, key: Optional[str]) -> ExplanationResult:
        model = getattr(self.explainer, "model_name", None)
        async with self.sem:
            res, retries = await self._retry(self.explainer.explain, w.fs, w.template, request_id=w.request.request_id, agent="explanation")
        if model:
            self.ledger.record(CallRecord(w.request.request_id, "explanation_agent", res.model, self.cfg.provider, res.llm_calls, res.tokens_input,
                                          res.tokens_output, res.duration_ms, False, res.error, retries=retries, fallback=not res.from_model))
        if key is not None and res.from_model and res.error is None:
            self.cfg.cache.put(key, {"text": res.text})
        return res

    async def _explain_batch(self, items: list[tuple[_Work, Optional[str]]]) -> dict[str, ExplanationResult]:
        out: dict[str, ExplanationResult] = {}
        if len(items) > 1 and hasattr(self.explainer, "explain_batch"):
            async with self.sem:
                results, usage = await self.explainer.explain_batch([(w.fs, w.template) for w, _ in items])
            self.ledger.record(CallRecord("batch:" + ",".join(w.request.request_id for w, _ in items), "explanation_agent", usage.model,
                                          self.cfg.provider, usage.llm_calls, usage.tokens_input, usage.tokens_output, usage.duration_ms,
                                          False, usage.error, batch_size=len(items)))
            for w, key in items:
                res = results.get(w.request.request_id)
                if res is not None:
                    out[w.request.request_id] = res
                    if key is not None:
                        self.cfg.cache.put(key, {"text": res.text})
        for w, key in items:
            if w.request.request_id not in out:
                self.ledger.note_request(w.request.request_id, batch_fallback_single="explanation")
                out[w.request.request_id] = await self._explain_single(w, key)
        return out

    async def _explanation_phase(self, works: list[_Work]) -> None:
        ready = [w for w in works if w.error is None and w.fs is not None]
        if not self.cfg.use_llm:
            for w in ready:
                w.explanation = ExplanationResult(text=w.template, from_model=False)
            return
        model = getattr(self.explainer, "model_name", None)
        pending: list[tuple[_Work, Optional[str]]] = []
        for w in ready:
            key = self._explanation_key(w.fs, w.template)
            hit = self.cfg.cache.get(key) if key else None
            if hit is not None:
                w.explanation = ExplanationResult(text=hit["text"], from_model=True, model=model)
                self.ledger.record(CallRecord(w.request.request_id, "explanation_agent", model, self.cfg.provider, 0, 0, 0, 0, True, None))
            else:
                pending.append((w, key))
        n = max(1, self.cfg.explanation_batch_size)
        batches = [pending[i:i + n] for i in range(0, len(pending), n)]
        results = await asyncio.gather(*(self._explain_batch(b) for b in batches), return_exceptions=True)
        for b, res in zip(batches, results):
            for w, _ in b:
                if isinstance(res, dict) and w.request.request_id in res:
                    w.explanation = res[w.request.request_id]
                else:
                    w.explanation = ExplanationResult(text=w.template, from_model=False, error=str(res) if not isinstance(res, dict) else "missing")

    # ------------------------------------------------------------------ finalize
    def _finalize(self, w: _Work) -> RequestOutcome:
        rid = w.request.request_id
        elapsed = int((time.time() - w.t0) * 1000)
        if w.error is not None or w.decision is None:
            ccy = self.ds.profile_by_user[w.request.user_id].home_currency.value if w.request.user_id in self.ds.profile_by_user else ""
            row = fallback_row(rid, w.safe, ccy, f"stage {w.stage}: {w.error}")
            self.ledger.note_request(rid, stage=w.stage, fallback=True, error=w.error)
            return RequestOutcome(row, rid, w.stage, w.evidence, [], w.error, elapsed)
        try:
            exp = w.explanation or ExplanationResult(text=w.template, from_model=False)
            w.stage = "consistency"
            row = row_from_decision(w.decision, exp.text)
            row = finalize(row, w.ctx, w.decision, w.state, w.base_path, w.fs.numbers,
                           lambda dd: template_explanation(dd, w.ctx, w.descriptions))
            self.ledger.note_request(rid, stage="done", fallback=row.fallback, repairs=row.repairs, status=row.affordability_status,
                                     method=row.recommended_payment_method, evidence_sources=len(w.ctx.evidence_sources),
                                     notes=w.ledger_notes, explanation_from_model=exp.from_model)
            return RequestOutcome(row, rid, "done", w.evidence, w.decision.notes, None, elapsed)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            ccy = self.ds.profile_by_user[w.request.user_id].home_currency.value
            row = fallback_row(rid, w.safe, ccy, f"stage {w.stage}: {err}")
            self.ledger.note_request(rid, stage=w.stage, fallback=True, error=err)
            return RequestOutcome(row, rid, w.stage, w.evidence, [], err, elapsed)

    # ------------------------------------------------------------------ batch entry points
    async def run(self, requests: Sequence[Request]) -> list[RequestOutcome]:
        works = [_Work(r) for r in requests]
        for w in works:  # phase 1: contexts
            try:
                w.ctx = build_context(self.ds, w.request)
            except Exception as exc:
                w.error = f"{type(exc).__name__}: {exc}"
        await self._evidence_phase([w for w in works if w.error is None])  # phase 2
        for w in works:  # phase 3
            if w.error is None:
                self._deterministic(w)
        await self._explanation_phase(works)  # phase 4
        return [self._finalize(w) for w in works]  # phase 5, input order

    async def process(self, request: Request) -> RequestOutcome:
        """Single-request convenience wrapper (same phases, batch of one)."""
        return (await self.run([request]))[0]
