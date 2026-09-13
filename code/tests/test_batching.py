"""Batching: several message sources per evidence call and several fact sheets per explanation
call, with per-item fallback to single calls. No network: stubbed extractor/explainer objects."""

from __future__ import annotations

import asyncio
from pathlib import Path

from buyorwait.agents.evidence_agent import EvidenceResult
from buyorwait.agents.explanation_agent import ExplanationResult
from buyorwait.agents.schemas import ValidatedEvidence
from buyorwait.data import load_dataset
from buyorwait.infra.usage import UsageLedger, build_usage_report
from buyorwait.pipeline import Pipeline, PipelineConfig
from conftest import SYNTHETIC, write_synthetic


def _dataset_with_two_messages(tmp_path: Path):
    msgs = [dict(m) for m in SYNTHETIC["messages.csv"]]
    msgs[1]["request_id"] = "request_B"
    msgs[1]["related_event_id"] = ""
    return load_dataset(write_synthetic(tmp_path, {"messages.csv": msgs}))


class StubExtractor:
    """Batch answers only for the sources named in ``batch_ok``; single calls always succeed."""

    model_name = "stub-model"

    def __init__(self, batch_ok: set[str], batch_error: str | None = None):
        self.batch_ok, self.batch_error = batch_ok, batch_error
        self.batch_calls, self.single_calls = 0, []

    async def extract_batch(self, pairs):
        self.batch_calls += 1
        if self.batch_error:
            return {}, EvidenceResult(evidence=ValidatedEvidence("batch", (), False, "", error=self.batch_error), model=self.model_name)
        out = {s.source_id: EvidenceResult(evidence=ValidatedEvidence(s.source_id, (), False, "batched"), model=self.model_name)
               for _, s in pairs if s.source_id in self.batch_ok}
        return out, EvidenceResult(evidence=ValidatedEvidence("batch", (), False, ""), model=self.model_name, llm_calls=1, tokens_input=300, tokens_output=40)

    async def extract(self, ctx, src):
        self.single_calls.append(src.source_id)
        return EvidenceResult(evidence=ValidatedEvidence(src.source_id, (), False, "single"), model=self.model_name, llm_calls=1, tokens_input=100, tokens_output=10)


class StubExplainer:
    model_name = "stub-model"

    def __init__(self, batch_ok: set[str]):
        self.batch_ok = batch_ok
        self.batch_calls, self.single_calls = 0, []

    async def explain_batch(self, items):
        self.batch_calls += 1
        out = {fs.request_id: ExplanationResult(text=f"batched {fs.request_id}", from_model=True, model=self.model_name)
               for fs, _ in items if fs.request_id in self.batch_ok}
        return out, ExplanationResult(text="", from_model=True, model=self.model_name, llm_calls=1, tokens_input=500, tokens_output=60)

    async def explain(self, fs, template):
        self.single_calls.append(fs.request_id)
        return ExplanationResult(text=template, from_model=False, model=self.model_name)


def test_evidence_batch_success_and_partial_fallback(tmp_path: Path):
    ds = _dataset_with_two_messages(tmp_path)
    # message_1 and message_2 are message-only sources -> one batch; image_1 goes alone
    ex = StubExtractor(batch_ok={"message_1"})  # message_2 omitted -> single-call fallback
    ledger = UsageLedger()
    pipe = Pipeline(ds, ex, StubExplainer(set()), ledger, PipelineConfig(use_llm=True, cache=None, evidence_batch_size=4, explanation_batch_size=1))
    outs = asyncio.run(pipe.run(list(ds.requests)))
    assert [o.request_id for o in outs] == ["request_A", "request_B"]
    assert ex.batch_calls == 1
    assert sorted(ex.single_calls) == ["image_1", "message_2"]  # image never batched; omitted item retried singly
    summaries = {e.source_id: e.summary for o in outs for e in o.evidence}
    assert summaries["message_1"] == "batched" and summaries["message_2"] == "single" and summaries["image_1"] == "single"
    assert not any(o.row.fallback for o in outs)
    report = build_usage_report(ledger, 2, "t")
    assert "Batched calls: 1 (covering 2 items)" in report


def test_evidence_batch_failure_falls_back_for_all(tmp_path: Path):
    ds = _dataset_with_two_messages(tmp_path)
    ex = StubExtractor(batch_ok=set(), batch_error="RuntimeError: batch model call failed")
    pipe = Pipeline(ds, ex, StubExplainer(set()), UsageLedger(), PipelineConfig(use_llm=True, cache=None, evidence_batch_size=4, explanation_batch_size=1))
    outs = asyncio.run(pipe.run(list(ds.requests)))
    assert ex.batch_calls == 1 and sorted(ex.single_calls) == ["image_1", "message_1", "message_2"]
    assert all(e.error is None for o in outs for e in o.evidence)


def test_explanation_batch_and_fallback(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    exp = StubExplainer(batch_ok={"request_A"})  # request_B missing from the batch -> single call
    pipe = Pipeline(ds, StubExtractor(set(), batch_error="x"), exp, UsageLedger(), PipelineConfig(use_llm=True, cache=None, evidence_batch_size=1, explanation_batch_size=8))
    outs = asyncio.run(pipe.run(list(ds.requests)))
    assert exp.batch_calls == 1 and exp.single_calls == ["request_B"]
    a, b = outs
    assert a.row.decision_explanation.startswith("batched request_A") or a.row.repairs  # grounded check may replace it
    assert not b.row.fallback


def test_batch_size_one_means_no_batching(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ex, exp = StubExtractor(set()), StubExplainer(set())
    pipe = Pipeline(ds, ex, exp, UsageLedger(), PipelineConfig(use_llm=True, cache=None, evidence_batch_size=1, explanation_batch_size=1))
    asyncio.run(pipe.run(list(ds.requests)))
    assert ex.batch_calls == 0 and exp.batch_calls == 0
