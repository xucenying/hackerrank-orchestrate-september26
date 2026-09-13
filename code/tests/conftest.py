"""Pytest configuration: make ``code/`` importable and provide shared fixtures."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from buyorwait.config import DATASET_DIR  # noqa: E402


@pytest.fixture(scope="session")
def real_dataset_dir() -> Path:
    if not (DATASET_DIR / "requests.csv").exists():
        pytest.skip("real dataset not present")
    return DATASET_DIR


# Minimal, internally consistent synthetic dataset. Tests copy and mutate it to trigger
# specific validation paths without touching the real files.
SYNTHETIC: dict[str, list[dict[str, str]]] = {
    "financial_profiles.csv": [
        {
            "user_id": "user_A", "home_currency": "INR", "current_available_balance": "1000",
            "minimum_balance_to_keep": "200", "financial_priorities": "education",
            "expense_categories_to_protect": "rent", "expense_categories_user_is_willing_to_reduce": "dining",
            "expense_categories_user_is_willing_to_stop": "streaming",
            "payment_methods_user_will_consider": "full_payment|installments", "max_installment_months": "3",
        },
        {
            "user_id": "user_B", "home_currency": "EUR", "current_available_balance": "50.5",
            "minimum_balance_to_keep": "10", "financial_priorities": "",
            "expense_categories_to_protect": "", "expense_categories_user_is_willing_to_reduce": "",
            "expense_categories_user_is_willing_to_stop": "",
            "payment_methods_user_will_consider": "partial_payment", "max_installment_months": "",
        },
    ],
    "financial_events.csv": [
        {
            "event_id": "event_1", "user_id": "user_A", "event_type": "expense", "description": "Rent",
            "category": "rent", "direction": "debit", "amount": "300", "currency": "INR",
            "event_date": "2026-01-04", "settlement_date": "2026-01-04", "status": "settled",
            "linked_event_id": "", "flexibility": "fixed", "minimum_allowed_amount": "",
        },
        {
            "event_id": "event_2", "user_id": "user_A", "event_type": "subscription", "description": "Streaming",
            "category": "streaming", "direction": "debit", "amount": "12.5", "currency": "INR",
            "event_date": "2026-01-10", "settlement_date": "", "status": "settled",
            "linked_event_id": "", "flexibility": "reducible_or_stoppable", "minimum_allowed_amount": "5",
        },
        {
            "event_id": "event_3", "user_id": "user_A", "event_type": "expense", "description": "Groceries",
            "category": "groceries", "direction": "debit", "amount": "", "currency": "INR",
            "event_date": "2026-02-01", "settlement_date": "2026-02-01", "status": "settled",
            "linked_event_id": "", "flexibility": "fixed", "minimum_allowed_amount": "",
        },
        {
            "event_id": "event_4", "user_id": "user_B", "event_type": "investment_valuation", "description": "Fund value",
            "category": "investment", "direction": "non_cash", "amount": "99", "currency": "EUR",
            "event_date": "2026-02-01", "settlement_date": "2026-02-01", "status": "unrealized",
            "linked_event_id": "event_5", "flexibility": "fixed", "minimum_allowed_amount": "",
        },
        {
            "event_id": "event_5", "user_id": "user_B", "event_type": "investment_purchase", "description": "Fund buy",
            "category": "investment", "direction": "debit", "amount": "80", "currency": "USD",
            "event_date": "2026-01-15", "settlement_date": "2026-01-15", "status": "settled",
            "linked_event_id": "", "flexibility": "fixed", "minimum_allowed_amount": "",
        },
    ],
    "exchange_rates.csv": [
        {"rate_date": "2026-01-15", "from_currency": "USD", "to_currency": "EUR", "rate": "0.92"},
    ],
    "requests.csv": [
        {
            "request_id": "request_A", "user_id": "user_A", "request_date": "2026-02-05", "request_type": "purchase",
            "requested_amount": "400", "desired_completion_date": "2026-03-05", "allows_partial_payment": "true",
            "request_text": "Can I buy this?",
        },
        {
            "request_id": "request_B", "user_id": "user_B", "request_date": "2026-02-05", "request_type": "other",
            "requested_amount": "20", "desired_completion_date": "2026-02-20", "allows_partial_payment": "false",
            "request_text": "Can I pay this?",
        },
    ],
    "sample_requests.csv": [
        {
            "request_id": "request_S", "user_id": "user_A", "request_date": "2026-01-20", "request_type": "travel",
            "requested_amount": "100", "desired_completion_date": "2026-02-20", "allows_partial_payment": "false",
            "request_text": "Trip?", "amount_safe_to_pay": "100", "affordability_status": "affordable_now",
            "recommended_payment_method": "full_payment", "payment_plan": "2026-01-20:100",
            "earliest_date_for_full_payment": "2026-01-20", "spending_changes_needed": "none",
            "decision_explanation": "Pay INR 100 today.",
        },
    ],
    "request_payment_options.csv": [
        {"payment_option_id": "payment_option_1", "request_id": "request_A", "payment_method": "full_payment",
         "payment_amount": "400", "number_of_payments": "1", "first_payment_date": "2026-02-05",
         "payment_frequency_days": "", "financing_fee": "0", "total_payable_amount": "400"},
        {"payment_option_id": "payment_option_2", "request_id": "request_A", "payment_method": "installments",
         "payment_amount": "140", "number_of_payments": "3", "first_payment_date": "2026-02-10",
         "payment_frequency_days": "30", "financing_fee": "20", "total_payable_amount": "420"},
        {"payment_option_id": "payment_option_3", "request_id": "request_B", "payment_method": "full_payment",
         "payment_amount": "20", "number_of_payments": "1", "first_payment_date": "2026-02-05",
         "payment_frequency_days": "", "financing_fee": "0", "total_payable_amount": "20"},
        {"payment_option_id": "payment_option_4", "request_id": "request_B", "payment_method": "installments",
         "payment_amount": "11", "number_of_payments": "2", "first_payment_date": "2026-02-05",
         "payment_frequency_days": "28", "financing_fee": "2", "total_payable_amount": "22"},
        {"payment_option_id": "payment_option_5", "request_id": "request_S", "payment_method": "full_payment",
         "payment_amount": "100", "number_of_payments": "1", "first_payment_date": "2026-01-20",
         "payment_frequency_days": "", "financing_fee": "0", "total_payable_amount": "100"},
        {"payment_option_id": "payment_option_6", "request_id": "request_S", "payment_method": "installments",
         "payment_amount": "52", "number_of_payments": "2", "first_payment_date": "2026-01-25",
         "payment_frequency_days": "28", "financing_fee": "4", "total_payable_amount": "104"},
    ],
    "messages.csv": [
        {"message_id": "message_1", "user_id": "user_A", "request_id": "request_A", "related_event_id": "",
         "sent_at": "2026-02-01T09:30:00Z", "source_type": "employer", "message_text": "Salary confirmed."},
        {"message_id": "message_2", "user_id": "user_B", "request_id": "", "related_event_id": "event_4",
         "sent_at": "2026-02-02T10:00:00Z", "source_type": "financial_service", "message_text": "No units sold."},
    ],
    "images.csv": [
        {"image_id": "image_1", "user_id": "user_A", "request_id": "request_A", "related_event_id": "event_3"},
    ],
    "output.csv": [
        {"request_id": "request_A", "amount_safe_to_pay": "", "affordability_status": "",
         "recommended_payment_method": "", "payment_plan": "", "earliest_date_for_full_payment": "",
         "spending_changes_needed": "", "decision_explanation": ""},
        {"request_id": "request_B", "amount_safe_to_pay": "", "affordability_status": "",
         "recommended_payment_method": "", "payment_plan": "", "earliest_date_for_full_payment": "",
         "spending_changes_needed": "", "decision_explanation": ""},
    ],
}

# 1x1 PNG so image-existence checks pass without shipping binary fixtures.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da"
    "6364f8cfc00000020001e221bc330000000049454e44ae426082"
)


def write_synthetic(tmp_path: Path, overrides: dict[str, list[dict[str, str]]] | None = None) -> Path:
    """Write the synthetic dataset (with optional per-file replacements) to ``tmp_path``."""
    data = {k: [dict(r) for r in v] for k, v in SYNTHETIC.items()}
    if overrides:
        data.update(overrides)
    for name, rows in data.items():
        cols = list(rows[0].keys()) if rows else []
        with (tmp_path / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    img_dir = tmp_path / "media" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    for row in data["images.csv"]:
        (img_dir / f"{row['image_id']}.png").write_bytes(_PNG_1X1)
    return tmp_path


@pytest.fixture
def synthetic_dir(tmp_path: Path) -> Path:
    return write_synthetic(tmp_path)
