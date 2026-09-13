"""Tests for stage 2/3a: FX conversion and the per-request context builder."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from buyorwait.config import FORECAST_HORIZON_DAYS
from buyorwait.data import load_dataset
from buyorwait.data.context import (
    CANDIDATE_LOOKBACK_DAYS,
    HISTORY_WINDOW_DAYS,
    MAX_CANDIDATES,
    build_all_contexts,
    build_context,
)
from buyorwait.data.fx import FxRateMissing, convert, lookup_rate
from buyorwait.data.models import Currency
from conftest import SYNTHETIC, write_synthetic


# --------------------------------------------------------------------------------------
# FX
# --------------------------------------------------------------------------------------


def test_fx_exact_date_and_direction(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    assert lookup_rate(ds.rate_by_key, date(2026, 1, 15), Currency.USD, Currency.EUR) == Decimal("0.92")
    assert lookup_rate(ds.rate_by_key, date(2026, 1, 15), Currency.EUR, Currency.EUR) == Decimal(1)
    assert convert(Decimal("80"), Currency.USD, Currency.EUR, date(2026, 1, 15), ds.rate_by_key) == Decimal("73.60")
    with pytest.raises(FxRateMissing):  # no inversion
        lookup_rate(ds.rate_by_key, date(2026, 1, 15), Currency.EUR, Currency.USD)
    with pytest.raises(FxRateMissing):  # no nearest-date fallback
        lookup_rate(ds.rate_by_key, date(2026, 1, 16), Currency.USD, Currency.EUR)


# --------------------------------------------------------------------------------------
# Context builder
# --------------------------------------------------------------------------------------


def test_context_windows_and_conversion(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctx = build_context(ds, ds.request_by_id["request_B"])
    assert ctx.horizon_start == date(2026, 2, 5)
    assert ctx.horizon_end == date(2026, 2, 5) + timedelta(days=FORECAST_HORIZON_DAYS)
    assert ctx.history_start == date(2026, 2, 5) - timedelta(days=HISTORY_WINDOW_DAYS)
    views = ctx.event_by_id
    # USD purchase converted to EUR on its settlement date
    assert views["event_5"].home_amount == Decimal("73.60") and views["event_5"].fx_rate == Decimal("0.92")
    # non_cash valuation is never converted
    assert views["event_4"].home_amount is None and views["event_4"].fx_rate is None and not views["event_4"].fx_missing
    assert views["event_5"].days_from_request == -21 and views["event_5"].is_history
    assert ctx.fx_issues == ()


def test_context_blank_amount_and_history_filter(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctx = build_context(ds, ds.request_by_id["request_A"])
    assert [v.event_id for v in ctx.events] == ["event_1", "event_2", "event_3"]
    assert ctx.event_by_id["event_3"].home_amount is None  # blank, not zero
    assert [v.event_id for v in ctx.blank_amount_events] == ["event_3"]
    assert [o.payment_option_id for o in ctx.options] == ["payment_option_1", "payment_option_2"]


def test_context_excludes_events_after_horizon(tmp_path: Path):
    rows = [dict(r) for r in SYNTHETIC["financial_events.csv"]]
    far = dict(rows[0])
    far.update(event_id="event_far", event_date="2026-09-01", settlement_date="2026-09-01", status="scheduled")
    rows.append(far)
    d = write_synthetic(tmp_path, {"financial_events.csv": rows})
    ds = load_dataset(d)
    ctx = build_context(ds, ds.request_by_id["request_A"])  # request 2026-02-05, horizon ends 2026-05-06
    assert "event_far" not in ctx.event_by_id


def test_context_fx_missing_is_recorded_not_fatal(tmp_path: Path):
    d = write_synthetic(tmp_path, {"exchange_rates.csv": [
        {"rate_date": "2025-12-15", "from_currency": "USD", "to_currency": "EUR", "rate": "0.9"},
    ]})
    ds = load_dataset(d, strict=False)
    ctx = build_context(ds, ds.request_by_id["request_B"])
    v = ctx.event_by_id["event_5"]
    assert v.fx_missing and v.home_amount is None
    assert ctx.fx_issues and "event_5" in ctx.fx_issues[0]


def test_context_messages_filtered_by_send_date(tmp_path: Path):
    msgs = [dict(m) for m in SYNTHETIC["messages.csv"]]
    msgs.append({"message_id": "message_late", "user_id": "user_A", "request_id": "request_A",
                 "related_event_id": "", "sent_at": "2026-02-06T00:00:00Z", "source_type": "bank",
                 "message_text": "Sent after the request."})
    d = write_synthetic(tmp_path, {"messages.csv": msgs})
    ds = load_dataset(d)
    ctx = build_context(ds, ds.request_by_id["request_A"])
    assert [m.message_id for m in ctx.messages] == ["message_1"]


def test_evidence_sources_and_candidates(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctx_a = build_context(ds, ds.request_by_id["request_A"])
    kinds = [(s.kind, s.source_id) for s in ctx_a.evidence_sources]
    assert kinds == [("message", "message_1"), ("image", "image_1")]
    msg, img = ctx_a.evidence_sources
    # message with no explicit event: window candidates, blank-amount row always included
    assert "event_3" in msg.candidate_event_ids and msg.explicit_event_id is None
    # image with explicit event: exactly that event (no links)
    assert img.candidate_event_ids == ("event_3",) and img.explicit_event_id == "event_3"
    assert img.image_path and img.image_path.endswith("image_1.png")

    ctx_b = build_context(ds, ds.request_by_id["request_B"])
    (src,) = ctx_b.evidence_sources
    # explicit event_4 links to event_5 -> both are candidates
    assert src.candidate_event_ids == ("event_4", "event_5")


def test_candidate_cap_is_deterministic(tmp_path: Path):
    base = dict(SYNTHETIC["financial_events.csv"][0])
    rows = []
    for k in range(MAX_CANDIDATES + 20):
        r = dict(base)
        day = date(2026, 2, 4) - timedelta(days=k)
        r.update(event_id=f"event_h{k:03d}", event_date=day.isoformat(), settlement_date=day.isoformat())
        rows.append(r)
    rows += [dict(r) for r in SYNTHETIC["financial_events.csv"][3:]]  # keep user_B rows
    d = write_synthetic(tmp_path, {"financial_events.csv": rows,
                                   "images.csv": [{"image_id": "image_1", "user_id": "user_B", "request_id": "request_B", "related_event_id": "event_5"}]})
    ds = load_dataset(d)
    ctx = build_context(ds, ds.request_by_id["request_A"])
    (src,) = ctx.evidence_sources
    assert len(src.candidate_event_ids) == MAX_CANDIDATES
    assert src.candidate_event_ids == tuple(sorted(src.candidate_event_ids, key=lambda i: ctx.event_by_id[i].effective_date))
    assert all(ctx.event_by_id[i].effective_date >= date(2026, 2, 1) - timedelta(days=CANDIDATE_LOOKBACK_DAYS) for i in src.candidate_event_ids)


def test_build_all_contexts_preserves_order(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir)
    ctxs = build_all_contexts(ds)
    assert [c.request_id for c in ctxs] == ["request_A", "request_B"]


# --------------------------------------------------------------------------------------
# Real dataset
# --------------------------------------------------------------------------------------


def test_real_contexts_build_for_every_request(real_dataset_dir: Path):
    ds = load_dataset(real_dataset_dir)
    ctxs = build_all_contexts(ds, list(ds.requests) + list(ds.sample_requests))
    assert len(ctxs) == 275
    assert all(not c.fx_issues for c in ctxs)
    assert all(c.options for c in ctxs)
    with_img = [c for c in ctxs if c.images]
    assert len(with_img) == 16
    for c in with_img:
        img_src = [s for s in c.evidence_sources if s.kind == "image"][0]
        assert img_src.explicit_event_id in img_src.candidate_event_ids
        assert Path(img_src.image_path).is_file()
    # every blank amount is visible to its request's context
    assert sum(len(c.blank_amount_events) for c in ctxs) == 16
