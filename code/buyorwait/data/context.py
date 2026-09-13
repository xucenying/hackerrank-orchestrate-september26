"""Per-request context builder (pipeline stage 2) and structured facts (stage 3a).

Given a validated ``Dataset`` and one request, assemble everything the later stages need,
using exact-key joins only:

* the user's profile
* the user's events up to the end of the 90-day horizon, each with a home-currency amount
* the request's seller payment options (sorted by option id)
* the user's messages and images, with deterministic candidate events for each
* the horizon and history windows

Nothing here interprets evidence, forecasts, or decides. It only selects and converts. The
result is a plain, immutable object that can be serialised for prompts and for tests.

Filtering rules (all deterministic, all documented here):

* Events: keep rows whose effective date is on or before ``request_date + HORIZON`` days. Rows
  after the horizon can never affect the forecast. Rows before the request are history.
* Messages: keep the user's messages sent on or before ``request_date``. A message sent after
  the request could not have been known when the question was asked. (In the supplied data
  every message predates its request; the rule exists for hidden inputs.)
* Images: keep the user's images. They are undated, so they are all kept.
* Candidate events for a message/image: if it names ``related_event_id``, that event plus any
  events linked to or from it. Otherwise the user's events whose effective date falls within
  ``CANDIDATE_LOOKBACK_DAYS`` before the message and the end of the horizon, capped at
  ``MAX_CANDIDATES`` most recent, always including pending/scheduled rows and blank-amount
  rows. This bounds prompt size without hiding the rows a message is likely to describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from ..config import FORECAST_HORIZON_DAYS
from .fx import FxRateMissing, event_home_amount
from .loader import Dataset
from .models import (
    Direction,
    EventStatus,
    FinancialEvent,
    FinancialProfile,
    ImageRef,
    Message,
    PaymentOption,
    Request,
)

# History window used later for recurrence detection. 180 days = six monthly cycles, enough to
# see a monthly pattern three times with room for a missed month; documented per CLAUDE.md §41.
HISTORY_WINDOW_DAYS: int = 180

# Evidence candidate selection (see module docstring). 90 days back covers a payroll notice
# sent up to a quarter before the request; 40 rows keeps the prompt small and deterministic.
CANDIDATE_LOOKBACK_DAYS: int = 90
MAX_CANDIDATES: int = 40


@dataclass(frozen=True)
class EventView:
    """A financial event as seen from one request: converted to home currency and positioned
    relative to the request date."""

    event: FinancialEvent
    home_amount: Optional[Decimal]  # None when the amount is blank or the rate is missing
    fx_rate: Optional[Decimal]  # 1 for same-currency rows
    fx_missing: bool  # True when a foreign amount exists but no dated rate was found
    days_from_request: int  # effective_date - request_date (negative = history)

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def effective_date(self) -> date:
        return self.event.effective_date

    @property
    def is_history(self) -> bool:
        return self.days_from_request < 0

    @property
    def is_future_or_today(self) -> bool:
        return self.days_from_request >= 0


@dataclass(frozen=True)
class EvidenceSource:
    """One message or image together with the events it may describe."""

    kind: str  # "message" | "image"
    source_id: str  # message_id or image_id
    message: Optional[Message]
    image: Optional[ImageRef]
    image_path: Optional[str]  # absolute path as str, only for images
    candidate_event_ids: tuple[str, ...]  # deterministic, ordered by effective date then id
    explicit_event_id: Optional[str]  # related_event_id when the row named one


@dataclass(frozen=True)
class RequestContext:
    request: Request
    profile: FinancialProfile
    horizon_start: date  # == request_date
    horizon_end: date  # request_date + FORECAST_HORIZON_DAYS
    history_start: date  # request_date - HISTORY_WINDOW_DAYS
    events: tuple[EventView, ...]  # sorted by effective date, then event_id
    options: tuple[PaymentOption, ...]  # sorted by option id
    messages: tuple[Message, ...]  # sorted by sent_at
    images: tuple[ImageRef, ...]
    evidence_sources: tuple[EvidenceSource, ...]
    fx_issues: tuple[str, ...] = field(default_factory=tuple)  # human-readable, for the run log
    rates: Mapping = field(default_factory=dict, repr=False, compare=False)  # dated rate table for evidence amounts

    # ---- convenience views -------------------------------------------------------------
    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def user_id(self) -> str:
        return self.request.user_id

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence_sources)

    @property
    def event_by_id(self) -> dict[str, EventView]:
        return {v.event_id: v for v in self.events}

    @property
    def history_events(self) -> tuple[EventView, ...]:
        return tuple(v for v in self.events if v.is_history and v.effective_date >= self.history_start)

    @property
    def future_events(self) -> tuple[EventView, ...]:
        return tuple(v for v in self.events if v.is_future_or_today)

    @property
    def blank_amount_events(self) -> tuple[EventView, ...]:
        return tuple(v for v in self.events if v.event.has_blank_amount)


# --------------------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------------------


def build_context(ds: Dataset, request: Request) -> RequestContext:
    profile = ds.profile_by_user.get(request.user_id)
    if profile is None:  # the loader flags this as an error; guard anyway for non-strict runs
        raise KeyError(f"{request.request_id}: no profile for {request.user_id}")

    horizon_start = request.request_date
    horizon_end = request.request_date + timedelta(days=FORECAST_HORIZON_DAYS)
    history_start = request.request_date - timedelta(days=HISTORY_WINDOW_DAYS)

    fx_issues: list[str] = []
    views: list[EventView] = []
    for e in ds.events_by_user.get(request.user_id, []):
        if e.effective_date > horizon_end:
            continue
        home, rate, missing = None, None, False
        if e.direction is not Direction.NON_CASH:
            try:
                home, rate = event_home_amount(e, profile, ds.rate_by_key)
            except FxRateMissing as exc:
                missing = True
                fx_issues.append(f"{e.event_id}: {exc}")
        views.append(
            EventView(
                event=e,
                home_amount=home,
                fx_rate=rate,
                fx_missing=missing,
                days_from_request=(e.effective_date - request.request_date).days,
            )
        )
    views.sort(key=lambda v: (v.effective_date, v.event.event_date, v.event_id))

    options = tuple(ds.options_by_request.get(request.request_id, []))

    messages = tuple(
        sorted(
            (m for m in ds.messages_by_user.get(request.user_id, []) if m.sent_at.date() <= request.request_date),
            key=lambda m: (m.sent_at, m.message_id),
        )
    )
    images = tuple(sorted(ds.images_by_user.get(request.user_id, []), key=lambda i: i.image_id))

    sources: list[EvidenceSource] = []
    for m in messages:
        sources.append(
            EvidenceSource(
                kind="message",
                source_id=m.message_id,
                message=m,
                image=None,
                image_path=None,
                candidate_event_ids=_candidates(views, ds, m.related_event_id, m.sent_at.date(), horizon_end),
                explicit_event_id=m.related_event_id,
            )
        )
    for i in images:
        sources.append(
            EvidenceSource(
                kind="image",
                source_id=i.image_id,
                message=None,
                image=i,
                image_path=str(ds.image_path(i)),
                candidate_event_ids=_candidates(views, ds, i.related_event_id, request.request_date, horizon_end),
                explicit_event_id=i.related_event_id,
            )
        )

    return RequestContext(
        request=request,
        profile=profile,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        history_start=history_start,
        events=tuple(views),
        options=options,
        messages=messages,
        images=images,
        evidence_sources=tuple(sources),
        fx_issues=tuple(fx_issues),
        rates=ds.rate_by_key,
    )


def _candidates(
    views: Sequence[EventView], ds: Dataset, explicit: Optional[str], anchor: date, horizon_end: date
) -> tuple[str, ...]:
    by_id = {v.event_id: v for v in views}
    if explicit is not None:
        ids = {explicit}
        ev = by_id.get(explicit)
        if ev is not None and ev.event.linked_event_id:
            ids.add(ev.event.linked_event_id)
        for v in views:  # rows that point back at the explicit event (refunds, re-attempts, valuations)
            if v.event.linked_event_id == explicit:
                ids.add(v.event_id)
        return tuple(sorted(i for i in ids if i in by_id))

    lo = anchor - timedelta(days=CANDIDATE_LOOKBACK_DAYS)
    always = [
        v for v in views
        if v.event.has_blank_amount or v.event.status in (EventStatus.PENDING, EventStatus.SCHEDULED)
    ]
    window = [v for v in views if lo <= v.effective_date <= horizon_end and v not in always]
    window = window[-max(0, MAX_CANDIDATES - len(always)):] if MAX_CANDIDATES > len(always) else []
    chosen = sorted(always + window, key=lambda v: (v.effective_date, v.event_id))
    return tuple(v.event_id for v in chosen)


def build_all_contexts(ds: Dataset, requests: Optional[Sequence[Request]] = None) -> list[RequestContext]:
    """Contexts for ``requests`` (default: the evaluation set) in input order."""
    return [build_context(ds, r) for r in (requests if requests is not None else ds.requests)]
