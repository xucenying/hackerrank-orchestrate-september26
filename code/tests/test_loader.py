"""Tests for stage 1: typed row models, CSV loading, and cross-file validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from buyorwait.data import DatasetValidationError, load_dataset
from buyorwait.data.models import (
    Currency,
    FinancialEvent,
    FinancialProfile,
    Flexibility,
    PaymentMethod,
    PaymentOption,
    Request,
    SampleRequest,
)
from conftest import SYNTHETIC, write_synthetic


# --------------------------------------------------------------------------------------
# Row models
# --------------------------------------------------------------------------------------


def test_profile_parses_lists_and_blank_months():
    p = FinancialProfile.model_validate(SYNTHETIC["financial_profiles.csv"][1])
    assert p.home_currency is Currency.EUR
    assert p.current_available_balance == Decimal("50.5")
    assert p.financial_priorities == ()
    assert p.payment_methods_user_will_consider == (PaymentMethod.PARTIAL_PAYMENT,)
    assert p.max_installment_months is None
    assert p.accepts_partial_payment and not p.accepts_full_payment and not p.accepts_installments


def test_profile_rejects_unknown_method_and_empty_methods():
    row = dict(SYNTHETIC["financial_profiles.csv"][0])
    row["payment_methods_user_will_consider"] = "full_payment|crypto"
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(row)
    row["payment_methods_user_will_consider"] = ""
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(row)


def test_event_blank_amount_is_none_not_zero():
    e = FinancialEvent.model_validate(SYNTHETIC["financial_events.csv"][2])
    assert e.amount is None and e.has_blank_amount


def test_event_effective_date_falls_back_to_event_date():
    e = FinancialEvent.model_validate(SYNTHETIC["financial_events.csv"][1])
    assert e.settlement_date is None
    assert e.effective_date == date(2026, 1, 10)
    assert e.flexibility is Flexibility.REDUCIBLE_OR_STOPPABLE
    assert e.flexibility.can_reduce and e.flexibility.can_stop


@pytest.mark.parametrize(
    "patch",
    [
        {"amount": "-5"},
        {"status": "done"},
        {"event_date": "2026/01/04"},
        {"direction": "non_cash"},  # non_cash must be unrealized
        {"currency": "GBP"},
    ],
)
def test_event_rejects_bad_values(patch):
    row = dict(SYNTHETIC["financial_events.csv"][0])
    row.update(patch)
    with pytest.raises(ValidationError):
        FinancialEvent.model_validate(row)


def test_request_bool_and_deadline_rule():
    r = Request.model_validate(SYNTHETIC["requests.csv"][0])
    assert r.allows_partial_payment is True
    bad = dict(SYNTHETIC["requests.csv"][0])
    bad["desired_completion_date"] = "2026-02-01"  # before request_date
    with pytest.raises(ValidationError):
        Request.model_validate(bad)


def test_sample_request_bounds():
    row = dict(SYNTHETIC["sample_requests.csv"][0])
    row["amount_safe_to_pay"] = "101"  # > requested_amount
    with pytest.raises(ValidationError):
        SampleRequest.model_validate(row)
    row["amount_safe_to_pay"] = "100"
    row["earliest_date_for_full_payment"] = ""
    s = SampleRequest.model_validate(row)
    assert s.earliest_date_for_full_payment is None


def test_payment_option_schedule_and_rules():
    o = PaymentOption.model_validate(SYNTHETIC["request_payment_options.csv"][1])
    assert o.schedule == (date(2026, 2, 10), date(2026, 3, 12), date(2026, 4, 11))
    assert o.last_payment_date == date(2026, 4, 11)
    bad = dict(SYNTHETIC["request_payment_options.csv"][1])
    bad["payment_frequency_days"] = ""  # multi-payment needs a frequency
    with pytest.raises(ValidationError):
        PaymentOption.model_validate(bad)
    bad = dict(SYNTHETIC["request_payment_options.csv"][0])
    bad["payment_method"] = "partial_payment"  # never a seller option
    with pytest.raises(ValidationError):
        PaymentOption.model_validate(bad)


def test_unknown_column_is_rejected():
    row = dict(SYNTHETIC["requests.csv"][0])
    row["extra"] = "x"
    with pytest.raises(ValidationError):
        Request.model_validate(row)


# --------------------------------------------------------------------------------------
# Loader on the synthetic dataset
# --------------------------------------------------------------------------------------


def test_synthetic_loads_clean(synthetic_dir: Path):
    ds = load_dataset(synthetic_dir, strict=True)
    assert ds.report.ok
    assert [r.request_id for r in ds.requests] == ["request_A", "request_B"]
    assert ds.request_by_id["request_S"].user_id == "user_A"
    assert [o.payment_option_id for o in ds.options_by_request["request_A"]] == ["payment_option_1", "payment_option_2"]
    assert ds.image_by_event["event_3"].image_id == "image_1"
    assert ds.messages_by_request["request_A"][0].message_id == "message_1"
    assert ds.messages_by_event["event_4"][0].message_id == "message_2"
    assert [e.event_id for e in ds.events_by_user["user_A"]] == ["event_1", "event_2", "event_3"]
    assert ds.rate_by_key[(date(2026, 1, 15), Currency.USD, Currency.EUR)].rate == Decimal("0.92")
    # warnings that the synthetic data deliberately triggers
    checks = {w.check for w in ds.report.warnings}
    assert "already_below_minimum" not in checks


def test_strict_raises_and_nonstrict_reports(tmp_path: Path):
    rows = [dict(r) for r in SYNTHETIC["financial_events.csv"]]
    rows[0]["user_id"] = "user_ZZZ"  # broken FK
    d = write_synthetic(tmp_path, {"financial_events.csv": rows})
    with pytest.raises(DatasetValidationError) as ei:
        load_dataset(d, strict=True)
    assert any(i.check == "fk_user" for i in ei.value.report.errors)
    ds = load_dataset(d, strict=False)
    assert not ds.report.ok
    assert "event_1" in ds.event_by_id  # FK failures are reported, row still loaded


def test_invalid_row_is_excluded_not_fatal(tmp_path: Path):
    rows = [dict(r) for r in SYNTHETIC["financial_events.csv"]]
    rows[1]["amount"] = "abc"
    d = write_synthetic(tmp_path, {"financial_events.csv": rows})
    ds = load_dataset(d, strict=False)
    assert "event_2" not in ds.event_by_id
    assert any(i.check == "row_invalid" and i.row_id == "event_2" for i in ds.report.errors)


def test_duplicate_id_is_error(tmp_path: Path):
    rows = [dict(r) for r in SYNTHETIC["requests.csv"]]
    rows.append(dict(rows[0]))
    d = write_synthetic(tmp_path, {"requests.csv": rows})
    ds = load_dataset(d, strict=False)
    assert any(i.check == "duplicate_id" for i in ds.report.errors)
    assert len(ds.requests) == 2


def test_missing_column_is_error(tmp_path: Path):
    rows = [{k: v for k, v in r.items() if k != "home_currency"} for r in SYNTHETIC["financial_profiles.csv"]]
    d = write_synthetic(tmp_path, {"financial_profiles.csv": rows})
    ds = load_dataset(d, strict=False)
    assert any(i.check == "columns" and i.file == "financial_profiles.csv" for i in ds.report.errors)
    assert ds.profiles == []


def test_blank_amount_without_image_is_error(tmp_path: Path):
    d = write_synthetic(tmp_path, {"images.csv": [
        {"image_id": "image_9", "user_id": "user_B", "request_id": "request_B", "related_event_id": "event_5"},
    ]})
    ds = load_dataset(d, strict=False)
    assert any(i.check == "blank_amount_no_image" and i.row_id == "event_3" for i in ds.report.errors)


def test_missing_image_file_is_error(synthetic_dir: Path):
    (synthetic_dir / "media" / "images" / "image_1.png").unlink()
    ds = load_dataset(synthetic_dir, strict=False)
    assert any(i.check == "image_file_missing" and i.row_id == "image_1" for i in ds.report.errors)


def test_request_without_options_is_error(tmp_path: Path):
    opts = [dict(o) for o in SYNTHETIC["request_payment_options.csv"] if o["request_id"] != "request_B"]
    d = write_synthetic(tmp_path, {"request_payment_options.csv": opts})
    ds = load_dataset(d, strict=False)
    assert any(i.check == "no_payment_options" and i.row_id == "request_B" for i in ds.report.errors)


def test_message_owner_mismatch_is_error(tmp_path: Path):
    msgs = [dict(m) for m in SYNTHETIC["messages.csv"]]
    msgs[0]["user_id"] = "user_B"  # request_A belongs to user_A
    d = write_synthetic(tmp_path, {"messages.csv": msgs})
    ds = load_dataset(d, strict=False)
    assert any(i.check == "request_other_user" and i.row_id == "message_1" for i in ds.report.errors)


def test_fx_missing_rate_is_warning(tmp_path: Path):
    d = write_synthetic(tmp_path, {"exchange_rates.csv": [
        {"rate_date": "2025-12-15", "from_currency": "USD", "to_currency": "EUR", "rate": "0.9"},
    ]})
    ds = load_dataset(d, strict=False)
    assert ds.report.ok
    assert any(w.check == "fx_rate_missing" and w.row_id == "event_5" for w in ds.report.warnings)


def test_option_arithmetic_warnings(tmp_path: Path):
    opts = [dict(o) for o in SYNTHETIC["request_payment_options.csv"]]
    opts[1]["total_payable_amount"] = "999"
    d = write_synthetic(tmp_path, {"request_payment_options.csv": opts})
    ds = load_dataset(d, strict=False)
    checks = {(w.check, w.row_id) for w in ds.report.warnings}
    assert ("total_mismatch", "payment_option_2") in checks
    assert ("fee_mismatch", "payment_option_2") in checks


def test_profile_preference_warnings(tmp_path: Path):
    profs = [dict(p) for p in SYNTHETIC["financial_profiles.csv"]]
    profs[0]["max_installment_months"] = ""  # accepts installments, no limit
    profs[1]["max_installment_months"] = "4"  # limit set, does not accept installments
    profs[1]["current_available_balance"] = "5"  # below its minimum of 10
    d = write_synthetic(tmp_path, {"financial_profiles.csv": profs})
    ds = load_dataset(d, strict=False)
    checks = {(w.check, w.row_id) for w in ds.report.warnings}
    assert ("installments_without_max_months", "user_A") in checks
    assert ("max_months_without_installments", "user_B") in checks
    assert ("already_below_minimum", "user_B") in checks


# --------------------------------------------------------------------------------------
# Loader on the real dataset (skipped if absent)
# --------------------------------------------------------------------------------------


def test_real_dataset_loads_without_errors(real_dataset_dir: Path):
    ds = load_dataset(real_dataset_dir, strict=True)
    assert len(ds.requests) == 250
    assert len(ds.sample_requests) == 25
    assert len(ds.profiles) == 275
    assert len(ds.images) == 16
    assert all(e.event_id in ds.image_by_event for e in ds.events if e.has_blank_amount)
    assert all(2 <= len(ds.options_by_request[r.request_id]) <= 4 for r in ds.requests)
    # template mirrors requests.csv
    assert not any(w.check == "template_order" for w in ds.report.warnings)
