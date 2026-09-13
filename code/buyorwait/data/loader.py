"""CSV loading and validation for ``dataset/``.

Responsibilities (pipeline stage 1):

1. Read each participant-facing CSV with the standard library (UTF-8, deterministic order).
2. Validate every row into its typed model (``models.py``). A bad row is recorded, not fatal.
3. Enforce cross-file integrity: unique ids, foreign keys, image files on disk, and a few
   semantic consistency rules that the later stages rely on.
4. Return a ``Dataset`` with indexed lookups plus a ``ValidationReport``.

Severity policy
---------------
* ``error``   - the row cannot be trusted; it is excluded from the ``Dataset``.
* ``warning`` - the row is kept, but something downstream should know about it.

``load_dataset(strict=True)`` raises ``DatasetValidationError`` if any error exists. This is the
mode the CLI uses so a corrupt input is noticed before any model call is made. Tests and the
evaluation harness can pass ``strict=False`` to inspect the report instead.

Nothing here reads organizer-only files. Nothing here decides what counts as cash.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import DATASET_DIR, IMAGES_DIR
from .models import (
    OUTPUT_COLUMNS,
    Currency,
    Direction,
    EventStatus,
    ExchangeRate,
    FinancialEvent,
    FinancialProfile,
    ImageRef,
    Message,
    PaymentMethod,
    PaymentOption,
    Request,
    SampleRequest,
)

RowT = TypeVar("RowT", bound=BaseModel)

# Exact header expected for each file (order-insensitive; extra or missing columns are errors).
EXPECTED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "financial_profiles.csv": tuple(FinancialProfile.model_fields),
    "financial_events.csv": tuple(FinancialEvent.model_fields),
    "exchange_rates.csv": tuple(ExchangeRate.model_fields),
    "requests.csv": tuple(Request.model_fields),
    "sample_requests.csv": tuple(SampleRequest.model_fields),
    "request_payment_options.csv": tuple(PaymentOption.model_fields),
    "messages.csv": tuple(Message.model_fields),
    "images.csv": tuple(ImageRef.model_fields),
    "output.csv": OUTPUT_COLUMNS,
}

# Tolerance for "n x amount == total" and "total == requested + fee" checks (currency minor unit).
_MONEY_TOLERANCE = Decimal("0.01")


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # "error" | "warning"
    file: str
    row_id: Optional[str]  # primary id of the offending row when known
    check: str  # short machine-readable check name
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        rid = f" [{self.row_id}]" if self.row_id else ""
        return f"{self.severity.upper()} {self.file}{rid} {self.check}: {self.detail}"


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)

    def add(self, severity: str, file: str, row_id: Optional[str], check: str, detail: str) -> None:
        self.issues.append(ValidationIssue(severity, file, row_id, check, detail))

    def error(self, file: str, row_id: Optional[str], check: str, detail: str) -> None:
        self.add("error", file, row_id, check, detail)

    def warning(self, file: str, row_id: Optional[str], check: str, detail: str) -> None:
        self.add("warning", file, row_id, check, detail)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines = [f"rows: " + ", ".join(f"{k}={v}" for k, v in self.row_counts.items())]
        lines.append(f"errors: {len(self.errors)}, warnings: {len(self.warnings)}")
        by_check: dict[tuple[str, str, str], int] = defaultdict(int)
        for i in self.issues:
            by_check[(i.severity, i.file, i.check)] += 1
        for (sev, f, chk), n in sorted(by_check.items()):
            lines.append(f"  {sev:<7} {f:<30} {chk:<32} x{n}")
        return "\n".join(lines)


class DatasetValidationError(RuntimeError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__(f"dataset has {len(report.errors)} validation error(s)\n{report.summary()}")


# --------------------------------------------------------------------------------------
# Dataset container with indexes
# --------------------------------------------------------------------------------------


@dataclass
class Dataset:
    dataset_dir: Path
    images_dir: Path
    profiles: list[FinancialProfile]
    events: list[FinancialEvent]
    exchange_rates: list[ExchangeRate]
    requests: list[Request]  # evaluation requests, in file order
    sample_requests: list[SampleRequest]
    payment_options: list[PaymentOption]
    messages: list[Message]
    images: list[ImageRef]
    report: ValidationReport

    # ---- indexes (built once, read-only by convention) ----
    profile_by_user: dict[str, FinancialProfile] = field(default_factory=dict)
    events_by_user: dict[str, list[FinancialEvent]] = field(default_factory=dict)
    event_by_id: dict[str, FinancialEvent] = field(default_factory=dict)
    rate_by_key: dict[tuple[date, Currency, Currency], ExchangeRate] = field(default_factory=dict)
    request_by_id: dict[str, Request] = field(default_factory=dict)  # eval + sample
    options_by_request: dict[str, list[PaymentOption]] = field(default_factory=dict)
    messages_by_user: dict[str, list[Message]] = field(default_factory=dict)
    messages_by_request: dict[str, list[Message]] = field(default_factory=dict)
    messages_by_event: dict[str, list[Message]] = field(default_factory=dict)
    images_by_user: dict[str, list[ImageRef]] = field(default_factory=dict)
    images_by_request: dict[str, list[ImageRef]] = field(default_factory=dict)
    image_by_event: dict[str, ImageRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.profile_by_user = {p.user_id: p for p in self.profiles}
        self.event_by_id = {e.event_id: e for e in self.events}
        by_user: dict[str, list[FinancialEvent]] = defaultdict(list)
        for e in self.events:
            by_user[e.user_id].append(e)
        # Stable chronological order per user (effective date, then event_id) for downstream code.
        self.events_by_user = {
            u: sorted(evs, key=lambda e: (e.effective_date, e.event_date, e.event_id))
            for u, evs in by_user.items()
        }
        self.rate_by_key = {r.key: r for r in self.exchange_rates}
        self.request_by_id = {r.request_id: r for r in self.requests}
        self.request_by_id.update({r.request_id: r for r in self.sample_requests})
        opts: dict[str, list[PaymentOption]] = defaultdict(list)
        for o in self.payment_options:
            opts[o.request_id].append(o)
        self.options_by_request = {k: sorted(v, key=lambda o: _option_sort_key(o)) for k, v in opts.items()}
        mu: dict[str, list[Message]] = defaultdict(list)
        mr: dict[str, list[Message]] = defaultdict(list)
        me: dict[str, list[Message]] = defaultdict(list)
        for m in self.messages:
            mu[m.user_id].append(m)
            if m.request_id:
                mr[m.request_id].append(m)
            if m.related_event_id:
                me[m.related_event_id].append(m)
        self.messages_by_user, self.messages_by_request, self.messages_by_event = dict(mu), dict(mr), dict(me)
        iu: dict[str, list[ImageRef]] = defaultdict(list)
        ir: dict[str, list[ImageRef]] = defaultdict(list)
        for i in self.images:
            iu[i.user_id].append(i)
            if i.request_id:
                ir[i.request_id].append(i)
            if i.related_event_id:
                self.image_by_event[i.related_event_id] = i
        self.images_by_user, self.images_by_request = dict(iu), dict(ir)

    def image_path(self, image: ImageRef) -> Path:
        return self.images_dir / image.filename


def _option_sort_key(o: PaymentOption) -> tuple[int, str]:
    """Sort options by numeric suffix of their id so payment_option_2 < payment_option_10."""
    suffix = o.payment_option_id.rsplit("_", 1)[-1]
    return (int(suffix) if suffix.isdigit() else 10**9, o.payment_option_id)


# --------------------------------------------------------------------------------------
# Reading and per-row validation
# --------------------------------------------------------------------------------------


def _read_csv(path: Path, report: ValidationReport) -> list[dict[str, str]]:
    name = path.name
    if not path.exists():
        report.error(name, None, "file_missing", f"{path} does not exist")
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = tuple(reader.fieldnames or ())
        expected = EXPECTED_COLUMNS[name]
        missing = [c for c in expected if c not in header]
        extra = [c for c in header if c not in expected]
        if missing or extra:
            report.error(name, None, "columns", f"missing={missing} extra={extra}")
            return []
        rows = list(reader)
    report.row_counts[name] = len(rows)
    return rows


def _validate_rows(
    rows: Iterable[dict[str, str]],
    model: type[RowT],
    file: str,
    id_field: str,
    report: ValidationReport,
) -> list[RowT]:
    out: list[RowT] = []
    seen: set[str] = set()
    for idx, raw in enumerate(rows, start=2):  # +2: header is line 1
        row_id = (raw.get(id_field) or "").strip() or f"line {idx}"
        try:
            obj = model.model_validate(raw)
        except ValidationError as exc:
            msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            report.error(file, row_id, "row_invalid", msgs)
            continue
        key = getattr(obj, id_field)
        if key in seen:
            report.error(file, row_id, "duplicate_id", f"{id_field} appears more than once")
            continue
        seen.add(key)
        out.append(obj)
    return out


# --------------------------------------------------------------------------------------
# Cross-file checks
# --------------------------------------------------------------------------------------


def _fk(
    report: ValidationReport,
    file: str,
    rows: Sequence[BaseModel],
    id_of: Callable[[BaseModel], str],
    ref_of: Callable[[BaseModel], Optional[str]],
    targets: set[str],
    check: str,
    severity: str = "error",
) -> None:
    for r in rows:
        ref = ref_of(r)
        if ref is not None and ref not in targets:
            report.add(severity, file, id_of(r), check, f"references unknown id {ref!r}")


def _cross_checks(ds: Dataset) -> None:
    rep = ds.report
    user_ids = set(ds.profile_by_user)
    event_ids = set(ds.event_by_id)
    all_request_ids = set(ds.request_by_id)

    # --- requests -------------------------------------------------------------------
    overlap = {r.request_id for r in ds.requests} & {s.request_id for s in ds.sample_requests}
    for rid in sorted(overlap):
        rep.error("requests.csv", rid, "id_overlap", "request_id also present in sample_requests.csv")
    _fk(rep, "requests.csv", ds.requests, lambda r: r.request_id, lambda r: r.user_id, user_ids, "fk_user")
    _fk(rep, "sample_requests.csv", ds.sample_requests, lambda r: r.request_id, lambda r: r.user_id, user_ids, "fk_user")
    for r in ds.requests + ds.sample_requests:
        fname = "sample_requests.csv" if isinstance(r, SampleRequest) else "requests.csv"
        prof = ds.profile_by_user.get(r.user_id)
        if prof is None:
            continue
        opts = ds.options_by_request.get(r.request_id, [])
        if not opts:
            rep.error(fname, r.request_id, "no_payment_options", "request has no rows in request_payment_options.csv")
            continue
        if not (2 <= len(opts) <= 4):
            rep.warning(fname, r.request_id, "option_count", f"expected 2-4 options, found {len(opts)}")
        fulls = [o for o in opts if o.payment_method is PaymentMethod.FULL_PAYMENT]
        if len(fulls) != 1:
            rep.warning(fname, r.request_id, "full_option_count", f"expected exactly one full_payment option, found {len(fulls)}")
        for o in fulls:
            if abs(o.payment_amount - r.requested_amount) > _MONEY_TOLERANCE:
                rep.warning("request_payment_options.csv", o.payment_option_id, "full_amount_mismatch",
                            f"full_payment amount {o.payment_amount} != requested {r.requested_amount}")
            if o.first_payment_date != r.request_date:
                rep.warning("request_payment_options.csv", o.payment_option_id, "full_date_mismatch",
                            f"full_payment date {o.first_payment_date} != request_date {r.request_date}")
        for o in opts:
            if o.first_payment_date < r.request_date:
                rep.warning("request_payment_options.csv", o.payment_option_id, "option_starts_before_request",
                            f"first payment {o.first_payment_date} precedes request_date {r.request_date}")

    # --- events ---------------------------------------------------------------------
    _fk(rep, "financial_events.csv", ds.events, lambda e: e.event_id, lambda e: e.user_id, user_ids, "fk_user")
    _fk(rep, "financial_events.csv", ds.events, lambda e: e.event_id, lambda e: e.linked_event_id, event_ids, "fk_linked_event")
    for e in ds.events:
        prof = ds.profile_by_user.get(e.user_id)
        if e.linked_event_id is not None:
            tgt = ds.event_by_id.get(e.linked_event_id)
            if tgt is not None and tgt.user_id != e.user_id:
                rep.error("financial_events.csv", e.event_id, "linked_event_other_user",
                          f"linked_event_id {e.linked_event_id} belongs to {tgt.user_id}")
        if e.has_blank_amount and e.event_id not in ds.image_by_event:
            rep.error("financial_events.csv", e.event_id, "blank_amount_no_image",
                      "amount is blank and no images.csv row references this event")
        if e.flexibility.can_reduce and e.minimum_allowed_amount is None:
            rep.warning("financial_events.csv", e.event_id, "reducible_without_floor",
                        "reducible event has no minimum_allowed_amount")
        if e.amount is not None and e.minimum_allowed_amount is not None and e.minimum_allowed_amount > e.amount:
            rep.warning("financial_events.csv", e.event_id, "floor_above_amount",
                        f"minimum_allowed_amount {e.minimum_allowed_amount} > amount {e.amount}")
        if e.settlement_date is not None and e.settlement_date < e.event_date:
            rep.warning("financial_events.csv", e.event_id, "settles_before_event",
                        f"settlement_date {e.settlement_date} < event_date {e.event_date}")
        if prof is not None and e.currency != prof.home_currency and e.direction is not Direction.NON_CASH:
            key = (e.effective_date, e.currency, prof.home_currency)
            if key not in ds.rate_by_key:
                rep.warning("financial_events.csv", e.event_id, "fx_rate_missing",
                            f"no rate for {e.currency}->{prof.home_currency} on {e.effective_date}")

    # --- exchange rates -------------------------------------------------------------
    seen_keys: set[tuple[date, Currency, Currency]] = set()
    for r in ds.exchange_rates:
        k = r.key
        if k in seen_keys:
            rep.error("exchange_rates.csv", f"{r.rate_date}:{r.from_currency}->{r.to_currency}", "duplicate_rate",
                      "same date and currency pair listed twice")
        seen_keys.add(k)

    # --- payment options ------------------------------------------------------------
    _fk(rep, "request_payment_options.csv", ds.payment_options, lambda o: o.payment_option_id,
        lambda o: o.request_id, all_request_ids, "fk_request")
    for o in ds.payment_options:
        if abs(o.payment_amount * o.number_of_payments - o.total_payable_amount) > _MONEY_TOLERANCE:
            rep.warning("request_payment_options.csv", o.payment_option_id, "total_mismatch",
                        f"{o.number_of_payments} x {o.payment_amount} != {o.total_payable_amount}")
        req = ds.request_by_id.get(o.request_id)
        if req is not None and abs(req.requested_amount + o.financing_fee - o.total_payable_amount) > _MONEY_TOLERANCE:
            rep.warning("request_payment_options.csv", o.payment_option_id, "fee_mismatch",
                        f"requested {req.requested_amount} + fee {o.financing_fee} != total {o.total_payable_amount}")

    # --- messages -------------------------------------------------------------------
    _fk(rep, "messages.csv", ds.messages, lambda m: m.message_id, lambda m: m.user_id, user_ids, "fk_user")
    _fk(rep, "messages.csv", ds.messages, lambda m: m.message_id, lambda m: m.request_id, all_request_ids, "fk_request")
    _fk(rep, "messages.csv", ds.messages, lambda m: m.message_id, lambda m: m.related_event_id, event_ids, "fk_event")
    for m in ds.messages:
        _owner_consistency(ds, "messages.csv", m.message_id, m.user_id, m.request_id, m.related_event_id)

    # --- images ---------------------------------------------------------------------
    _fk(rep, "images.csv", ds.images, lambda i: i.image_id, lambda i: i.user_id, user_ids, "fk_user")
    _fk(rep, "images.csv", ds.images, lambda i: i.image_id, lambda i: i.request_id, all_request_ids, "fk_request")
    _fk(rep, "images.csv", ds.images, lambda i: i.image_id, lambda i: i.related_event_id, event_ids, "fk_event")
    event_image_count: dict[str, int] = defaultdict(int)
    for i in ds.images:
        _owner_consistency(ds, "images.csv", i.image_id, i.user_id, i.request_id, i.related_event_id)
        if not ds.image_path(i).is_file():
            rep.error("images.csv", i.image_id, "image_file_missing", f"{ds.image_path(i)} not found")
        if i.related_event_id:
            event_image_count[i.related_event_id] += 1
    for eid, n in event_image_count.items():
        if n > 1:
            rep.warning("images.csv", eid, "multiple_images_per_event", f"{n} images reference the same event")

    # --- profiles: preference consistency ------------------------------------------
    for p in ds.profiles:
        if p.accepts_installments and p.max_installment_months is None:
            rep.warning("financial_profiles.csv", p.user_id, "installments_without_max_months",
                        "user accepts installments but max_installment_months is blank")
        if not p.accepts_installments and p.max_installment_months is not None:
            rep.warning("financial_profiles.csv", p.user_id, "max_months_without_installments",
                        "max_installment_months set but user does not accept installments")
        if p.current_available_balance < p.minimum_balance_to_keep:
            rep.warning("financial_profiles.csv", p.user_id, "already_below_minimum",
                        f"balance {p.current_available_balance} < minimum {p.minimum_balance_to_keep}")


def _owner_consistency(
    ds: Dataset, file: str, row_id: str, user_id: str, request_id: Optional[str], event_id: Optional[str]
) -> None:
    """A message/image that names a request or event must belong to the same user."""
    rep = ds.report
    if request_id:
        req = ds.request_by_id.get(request_id)
        if req is not None and req.user_id != user_id:
            rep.error(file, row_id, "request_other_user", f"{request_id} belongs to {req.user_id}, row says {user_id}")
    if event_id:
        ev = ds.event_by_id.get(event_id)
        if ev is not None and ev.user_id != user_id:
            rep.error(file, row_id, "event_other_user", f"{event_id} belongs to {ev.user_id}, row says {user_id}")


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def load_dataset(
    dataset_dir: Path | str = DATASET_DIR,
    images_dir: Path | str | None = None,
    *,
    strict: bool = True,
) -> Dataset:
    """Load and validate every participant-facing file.

    Args:
        dataset_dir: directory containing the CSVs (defaults to ``<repo>/dataset``).
        images_dir: directory containing ``<image_id>.png`` files (defaults to ``dataset/media/images``).
        strict: raise ``DatasetValidationError`` when any error-level issue exists.

    Returns:
        A ``Dataset`` whose ``report`` lists every issue found. Rows that failed validation are
        excluded; everything else is indexed for O(1) lookups.
    """
    dataset_dir = Path(dataset_dir)
    images_dir = Path(images_dir) if images_dir is not None else (
        IMAGES_DIR if dataset_dir == DATASET_DIR else dataset_dir / "media" / "images"
    )
    report = ValidationReport()

    def read(name: str) -> list[dict[str, str]]:
        return _read_csv(dataset_dir / name, report)

    profiles = _validate_rows(read("financial_profiles.csv"), FinancialProfile, "financial_profiles.csv", "user_id", report)
    events = _validate_rows(read("financial_events.csv"), FinancialEvent, "financial_events.csv", "event_id", report)
    rates = _validate_rates(read("exchange_rates.csv"), report)
    requests = _validate_rows(read("requests.csv"), Request, "requests.csv", "request_id", report)
    samples = _validate_rows(read("sample_requests.csv"), SampleRequest, "sample_requests.csv", "request_id", report)
    options = _validate_rows(read("request_payment_options.csv"), PaymentOption, "request_payment_options.csv", "payment_option_id", report)
    messages = _validate_rows(read("messages.csv"), Message, "messages.csv", "message_id", report)
    images = _validate_rows(read("images.csv"), ImageRef, "images.csv", "image_id", report)

    # output.csv is only a template; verify its header and that it mirrors requests.csv.
    template_rows = read("output.csv")
    if template_rows:
        template_ids = [r["request_id"] for r in template_rows]
        if template_ids != [r.request_id for r in requests]:
            report.warning("output.csv", None, "template_order", "template request_ids differ from requests.csv order")

    ds = Dataset(
        dataset_dir=dataset_dir,
        images_dir=images_dir,
        profiles=profiles,
        events=events,
        exchange_rates=rates,
        requests=requests,
        sample_requests=samples,
        payment_options=options,
        messages=messages,
        images=images,
        report=report,
    )
    _cross_checks(ds)

    if strict and not report.ok:
        raise DatasetValidationError(report)
    return ds


def _validate_rates(rows: Iterable[dict[str, str]], report: ValidationReport) -> list[ExchangeRate]:
    """Exchange rates have a composite key, so they bypass the single-id path."""
    out: list[ExchangeRate] = []
    for idx, raw in enumerate(rows, start=2):
        row_id = f"line {idx}"
        try:
            out.append(ExchangeRate.model_validate(raw))
        except ValidationError as exc:
            msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            report.error("exchange_rates.csv", row_id, "row_invalid", msgs)
    return out
