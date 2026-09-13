"""Tests for stage 3b: evidence schema, prompt assembly, and code-owned validation.

No network. The PicoAgents extractor is exercised only through a stubbed model client.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from buyorwait.agents import (
    NullEvidenceExtractor,
    StructuredEvidence,
    build_evidence_messages,
    validate_evidence,
)
from buyorwait.agents.evidence_agent import facts_block, load_system_prompt, untrusted_block
from buyorwait.agents.schemas import EvidenceItem
from buyorwait.data import load_dataset
from buyorwait.data.context import build_context


def _item(**kw) -> EvidenceItem:
    base = dict(kind="confirmation", target_event_id="event_3", confidence="high", source_ref="image_1", quote="x")
    base.update(kw)
    return EvidenceItem(**base)


@pytest.fixture
def ctx_a(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    return build_context(ds, ds.request_by_id["request_A"])


def test_schema_is_strict_json_friendly():
    schema = StructuredEvidence.model_json_schema()
    # nested item schema is referenced via $defs; kinds and confidence are enums
    item = schema["$defs"]["EvidenceItem"]
    assert set(item["properties"]) == {
        "kind", "target_event_id", "new_amount", "currency", "new_date", "effective_from",
        "confidence", "source_ref", "quote",
    }
    assert "unresolved" in item["properties"]["kind"]["enum"]
    assert item["additionalProperties"] is False
    with pytest.raises(Exception):
        EvidenceItem(kind="bogus", confidence="high", source_ref="m", quote="q")


def test_prompt_blocks(ctx_a):
    msg_src, img_src = ctx_a.evidence_sources
    facts = facts_block(ctx_a, msg_src)
    assert "request_id=request_A" in facts and "\nevent_3|" in facts
    assert "|BLANK INR|" in facts  # blank stays blank, never 0
    u = untrusted_block(msg_src)
    assert u.startswith('<untrusted_evidence source_id="message_1">') and "Salary confirmed." in u
    sys_prompt = load_system_prompt()
    assert "<untrusted_evidence>" in sys_prompt and "injection_detected" in sys_prompt


def test_build_messages_text_and_image(ctx_a):
    pytest.importorskip("picoagents")
    from picoagents.messages import MultiModalMessage, UserMessage

    msg_src, img_src = ctx_a.evidence_sources
    m1 = build_evidence_messages(ctx_a, msg_src)
    assert len(m1) == 1 and isinstance(m1[0], UserMessage) and "<untrusted_evidence" in m1[0].content
    m2 = build_evidence_messages(ctx_a, img_src)
    assert len(m2) == 2 and isinstance(m2[1], MultiModalMessage) and m2[1].mime_type == "image/png"
    assert m2[1].is_image() and m2[1].to_base64()


def test_validation_accepts_good_extraction(ctx_a):
    _, img_src = ctx_a.evidence_sources
    raw = StructuredEvidence(
        items=[_item(kind="amount_extraction", new_amount=2854.0, currency="INR", quote="Item Bill 2854.00")],
        injection_detected=False, summary="grocery receipt",
    )
    v = validate_evidence(raw, ctx_a, img_src)
    (it,) = v.items
    assert it.usable and it.kind == "amount_extraction"
    assert it.new_amount == Decimal("2854.0") and isinstance(it.new_amount, Decimal)
    assert it.rejection_reason is None


@pytest.mark.parametrize(
    "patch, reason_fragment",
    [
        ({"target_event_id": "event_999"}, "not among candidates"),
        ({"target_event_id": None, "kind": "cancellation"}, "requires target_event_id"),
        ({"kind": "amount_extraction", "new_amount": 10.0, "currency": "USD"}, "!= event currency"),
        ({"kind": "amount_extraction", "new_amount": None, "currency": "INR"}, "requires new_amount"),
        ({"kind": "amendment", "new_amount": -1.0, "currency": "INR"}, "negative amount"),
        ({"kind": "delay", "new_date": "12/03/2026"}, "bad date"),
        ({"kind": "income_change", "target_event_id": None, "new_amount": 5.0, "currency": "INR"}, "requires effective_from"),
        ({"confidence": "medium"}, "confidence medium"),
        ({"confidence": "low"}, "confidence low"),
        ({"source_ref": "message_1"}, "source_ref"),
        ({"currency": "GBP", "kind": "amendment", "new_amount": 1.0}, "unknown currency"),
    ],
)
def test_validation_downgrades_bad_items(ctx_a, patch, reason_fragment):
    _, img_src = ctx_a.evidence_sources
    raw = StructuredEvidence(items=[_item(**patch)], injection_detected=False, summary="")
    (it,) = validate_evidence(raw, ctx_a, img_src).items
    assert it.kind == "unresolved" and not it.usable
    assert reason_fragment in (it.rejection_reason or "")
    assert it.original_kind == patch.get("kind", "confirmation")


def test_validation_rejects_extraction_on_event_with_amount(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctx = build_context(ds, ds.request_by_id["request_B"])
    (src,) = ctx.evidence_sources  # message_2 -> event_4 (+ linked event_5, which has an amount)
    raw = StructuredEvidence(
        items=[_item(kind="amount_extraction", target_event_id="event_5", new_amount=80.0, currency="USD", source_ref="message_2")],
        injection_detected=False, summary="",
    )
    (it,) = validate_evidence(raw, ctx, src).items
    assert it.kind == "unresolved" and "already has an amount" in it.rejection_reason


def test_income_change_without_event_is_valid(ctx_a):
    msg_src, _ = ctx_a.evidence_sources
    raw = StructuredEvidence(
        items=[_item(kind="income_change", target_event_id=None, new_amount=42750000.0, currency="IDR",
                     effective_from="2026-03-15", source_ref="message_1", quote="naik menjadi IDR 42750000")],
        injection_detected=False, summary="payroll change",
    )
    (it,) = validate_evidence(raw, ctx_a, msg_src).items
    assert it.usable and it.effective_from == date(2026, 3, 15) and it.target_event_id is None


def test_null_extractor_skips(ctx_a):
    msg_src, _ = ctx_a.evidence_sources
    res = asyncio.run(NullEvidenceExtractor().extract(ctx_a, msg_src))
    assert res.evidence.skipped and res.evidence.items == () and res.llm_calls == 0


def test_pico_extractor_with_stub_client_and_failure(ctx_a):
    """Drive PicoEvidenceExtractor through a fake model client: one good call, one exception."""
    pytest.importorskip("picoagents")
    from picoagents.llm import BaseChatCompletionClient
    from picoagents.messages import AssistantMessage
    from picoagents.types import ChatCompletionResult, Usage

    from buyorwait.agents import PicoEvidenceExtractor

    good = StructuredEvidence(
        items=[_item(kind="amount_extraction", new_amount=2854.0, currency="INR", quote="Item Bill 2854.00")],
        injection_detected=False, summary="receipt",
    )

    class StubClient(BaseChatCompletionClient):
        def __init__(self, fail: bool):
            super().__init__(model="stub-model")
            self.fail = fail
            self.calls = 0

        async def create(self, messages, tools=None, output_format=None, **kw):
            self.calls += 1
            if self.fail:
                raise RuntimeError("simulated API outage")
            assert output_format is StructuredEvidence
            assert any(getattr(m, "role", "") == "system" for m in messages)
            return ChatCompletionResult(
                message=AssistantMessage(content=good.model_dump_json(), source="stub", structured_content=good),
                usage=Usage(duration_ms=5, llm_calls=1, tokens_input=100, tokens_output=20),
                model="stub-model", finish_reason="stop", structured_output=good,
            )

        async def create_stream(self, messages, tools=None, output_format=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def _to_config(self):  # pragma: no cover
            raise NotImplementedError

        @classmethod
        def _from_config(cls, config):  # pragma: no cover
            raise NotImplementedError

    _, img_src = ctx_a.evidence_sources
    ok_client = StubClient(fail=False)
    res = asyncio.run(PicoEvidenceExtractor(ok_client).extract(ctx_a, img_src))
    assert ok_client.calls == 1
    assert res.evidence.error is None and res.evidence.usable_items[0].new_amount == Decimal("2854.0")
    assert res.llm_calls == 1 and res.tokens_input == 100 and res.model == "stub-model"

    bad_client = StubClient(fail=True)
    res = asyncio.run(PicoEvidenceExtractor(bad_client).extract(ctx_a, img_src))
    assert res.evidence.items == () and "simulated API outage" in (res.evidence.error or "")
