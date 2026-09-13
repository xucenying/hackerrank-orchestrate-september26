"""Typed row models for every participant-facing CSV in ``dataset/``.

Design notes
------------
* One Pydantic model per CSV. Field names match the CSV headers exactly so rows can be
  validated with ``Model.model_validate(row_dict)``.
* Money is ``Decimal`` (never float). Dates are ``datetime.date``. Blank cells become ``None``.
* Enumerations are closed ``StrEnum`` types so an unexpected value fails validation instead of
  silently flowing into the financial engine.
* Pipe-separated profile lists are parsed into tuples in declaration order.
* These models describe *what the file says*. They do not decide what counts as cash; that
  is the reconciliation stage's job.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------------------
# Enumerations (closed vocabularies observed in the dataset and defined by the spec)
# --------------------------------------------------------------------------------------


class Currency(StrEnum):
    INR = "INR"
    ZAR = "ZAR"
    IDR = "IDR"
    USD = "USD"
    EUR = "EUR"


class EventType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    SUBSCRIPTION = "subscription"
    DEBT_PAYMENT = "debt_payment"
    REFUND = "refund"
    INVESTMENT_PURCHASE = "investment_purchase"
    INVESTMENT_SALE = "investment_sale"
    INVESTMENT_VALUATION = "investment_valuation"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    NON_CASH = "non_cash"


class EventStatus(StrEnum):
    SETTLED = "settled"
    PENDING = "pending"
    SCHEDULED = "scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNREALIZED = "unrealized"


class Flexibility(StrEnum):
    FIXED = "fixed"
    REDUCIBLE = "reducible"
    STOPPABLE = "stoppable"
    REDUCIBLE_OR_STOPPABLE = "reducible_or_stoppable"

    @property
    def can_reduce(self) -> bool:
        return self in (Flexibility.REDUCIBLE, Flexibility.REDUCIBLE_OR_STOPPABLE)

    @property
    def can_stop(self) -> bool:
        return self in (Flexibility.STOPPABLE, Flexibility.REDUCIBLE_OR_STOPPABLE)


class RequestType(StrEnum):
    PURCHASE = "purchase"
    TRAVEL = "travel"
    EDUCATION = "education"
    FAMILY_TRANSFER = "family_transfer"
    DEBT_REPAYMENT = "debt_repayment"
    INVESTMENT = "investment"
    HOUSING = "housing"
    EMERGENCY_EXPENSE = "emergency_expense"
    OTHER = "other"


class PaymentMethod(StrEnum):
    """Methods a user may list in ``payment_methods_user_will_consider`` and that a seller
    option may carry. ``wait``/``not_recommended`` are output-only and live in ``OutputMethod``."""

    FULL_PAYMENT = "full_payment"
    PARTIAL_PAYMENT = "partial_payment"
    INSTALLMENTS = "installments"


class OutputMethod(StrEnum):
    FULL_PAYMENT = "full_payment"
    PARTIAL_PAYMENT = "partial_payment"
    INSTALLMENTS = "installments"
    WAIT = "wait"
    NOT_RECOMMENDED = "not_recommended"


class AffordabilityStatus(StrEnum):
    AFFORDABLE_NOW = "affordable_now"
    AFFORDABLE_WITH_PLAN = "affordable_with_plan"
    AFFORDABLE_LATER = "affordable_later"
    NOT_AFFORDABLE = "not_affordable"


class MessageSource(StrEnum):
    EMPLOYER = "employer"
    BANK = "bank"
    MERCHANT = "merchant"
    FINANCIAL_SERVICE = "financial_service"
    SERVICE_PROVIDER = "service_provider"


# --------------------------------------------------------------------------------------
# Shared parsing helpers
# --------------------------------------------------------------------------------------


def _blank_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _parse_decimal(value: Any, field: str) -> Optional[Decimal]:
    value = _blank_to_none(value)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:  # pragma: no cover - exercised via tests
        raise ValueError(f"{field}: not a decimal number: {value!r}") from exc


def _parse_date(value: Any, field: str) -> Optional[date]:
    value = _blank_to_none(value)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field}: not a YYYY-MM-DD date: {value!r}") from exc


def _parse_pipe_list(value: Any) -> tuple[str, ...]:
    value = _blank_to_none(value)
    if value is None:
        return ()
    return tuple(part.strip() for part in str(value).split("|") if part.strip())


def _parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValueError(f"{field}: not a boolean: {value!r}")


class _Row(BaseModel):
    """Base for all CSV row models: forbid unknown columns, strip whitespace, freeze."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


# --------------------------------------------------------------------------------------
# financial_profiles.csv
# --------------------------------------------------------------------------------------


class FinancialProfile(_Row):
    user_id: str
    home_currency: Currency
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...] = ()
    expense_categories_to_protect: tuple[str, ...] = ()
    expense_categories_user_is_willing_to_reduce: tuple[str, ...] = ()
    expense_categories_user_is_willing_to_stop: tuple[str, ...] = ()
    payment_methods_user_will_consider: tuple[PaymentMethod, ...]
    max_installment_months: Optional[int] = None

    @field_validator("current_available_balance", "minimum_balance_to_keep", mode="before")
    @classmethod
    def _money(cls, v: Any, info: Any) -> Decimal:
        parsed = _parse_decimal(v, info.field_name)
        if parsed is None:
            raise ValueError(f"{info.field_name}: required")
        return parsed

    @field_validator(
        "financial_priorities",
        "expense_categories_to_protect",
        "expense_categories_user_is_willing_to_reduce",
        "expense_categories_user_is_willing_to_stop",
        mode="before",
    )
    @classmethod
    def _lists(cls, v: Any) -> tuple[str, ...]:
        return _parse_pipe_list(v)

    @field_validator("payment_methods_user_will_consider", mode="before")
    @classmethod
    def _methods(cls, v: Any) -> tuple[str, ...]:
        parts = _parse_pipe_list(v)
        if not parts:
            raise ValueError("payment_methods_user_will_consider: must list at least one method")
        return parts

    @field_validator("max_installment_months", mode="before")
    @classmethod
    def _months(cls, v: Any) -> Optional[int]:
        v = _blank_to_none(v)
        if v is None:
            return None
        n = int(str(v).strip())
        if n <= 0:
            raise ValueError("max_installment_months: must be positive when present")
        return n

    @model_validator(mode="after")
    def _checks(self) -> "FinancialProfile":
        if self.minimum_balance_to_keep < 0:
            raise ValueError("minimum_balance_to_keep: must be >= 0")
        return self

    @property
    def accepts_full_payment(self) -> bool:
        return PaymentMethod.FULL_PAYMENT in self.payment_methods_user_will_consider

    @property
    def accepts_partial_payment(self) -> bool:
        return PaymentMethod.PARTIAL_PAYMENT in self.payment_methods_user_will_consider

    @property
    def accepts_installments(self) -> bool:
        return PaymentMethod.INSTALLMENTS in self.payment_methods_user_will_consider


# --------------------------------------------------------------------------------------
# financial_events.csv
# --------------------------------------------------------------------------------------


class FinancialEvent(_Row):
    event_id: str
    user_id: str
    event_type: EventType
    description: str
    category: str
    direction: Direction
    amount: Optional[Decimal] = None  # blank => must be resolved from an image (never zero)
    currency: Currency
    event_date: date
    settlement_date: Optional[date] = None
    status: EventStatus
    linked_event_id: Optional[str] = None
    flexibility: Flexibility
    minimum_allowed_amount: Optional[Decimal] = None

    @field_validator("amount", "minimum_allowed_amount", mode="before")
    @classmethod
    def _money(cls, v: Any, info: Any) -> Optional[Decimal]:
        return _parse_decimal(v, info.field_name)

    @field_validator("event_date", mode="before")
    @classmethod
    def _event_date(cls, v: Any) -> date:
        parsed = _parse_date(v, "event_date")
        if parsed is None:
            raise ValueError("event_date: required")
        return parsed

    @field_validator("settlement_date", mode="before")
    @classmethod
    def _settlement_date(cls, v: Any) -> Optional[date]:
        return _parse_date(v, "settlement_date")

    @field_validator("linked_event_id", mode="before")
    @classmethod
    def _linked(cls, v: Any) -> Optional[str]:
        return _blank_to_none(v)

    @model_validator(mode="after")
    def _checks(self) -> "FinancialEvent":
        if self.amount is not None and self.amount < 0:
            raise ValueError("amount: must be >= 0 (direction carries the sign)")
        if self.minimum_allowed_amount is not None and self.minimum_allowed_amount < 0:
            raise ValueError("minimum_allowed_amount: must be >= 0")
        if self.direction is Direction.NON_CASH and self.status is not EventStatus.UNREALIZED:
            raise ValueError("non_cash rows must have status unrealized")
        if self.status is EventStatus.UNREALIZED and self.direction is not Direction.NON_CASH:
            raise ValueError("unrealized rows must have direction non_cash")
        return self

    @property
    def effective_date(self) -> date:
        """Settlement date when present, else event date (cash moves on settlement)."""
        return self.settlement_date or self.event_date

    @property
    def has_blank_amount(self) -> bool:
        return self.amount is None


# --------------------------------------------------------------------------------------
# exchange_rates.csv
# --------------------------------------------------------------------------------------


class ExchangeRate(_Row):
    rate_date: date
    from_currency: Currency
    to_currency: Currency
    rate: Decimal

    @field_validator("rate_date", mode="before")
    @classmethod
    def _d(cls, v: Any) -> date:
        parsed = _parse_date(v, "rate_date")
        if parsed is None:
            raise ValueError("rate_date: required")
        return parsed

    @field_validator("rate", mode="before")
    @classmethod
    def _r(cls, v: Any) -> Decimal:
        parsed = _parse_decimal(v, "rate")
        if parsed is None or parsed <= 0:
            raise ValueError("rate: must be a positive number")
        return parsed

    @model_validator(mode="after")
    def _checks(self) -> "ExchangeRate":
        if self.from_currency == self.to_currency:
            raise ValueError("from_currency and to_currency must differ")
        return self

    @property
    def key(self) -> tuple[date, Currency, Currency]:
        return (self.rate_date, self.from_currency, self.to_currency)


# --------------------------------------------------------------------------------------
# requests.csv and sample_requests.csv
# --------------------------------------------------------------------------------------


class Request(_Row):
    request_id: str
    user_id: str
    request_date: date
    request_type: RequestType
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

    @field_validator("request_date", "desired_completion_date", mode="before")
    @classmethod
    def _d(cls, v: Any, info: Any) -> date:
        parsed = _parse_date(v, info.field_name)
        if parsed is None:
            raise ValueError(f"{info.field_name}: required")
        return parsed

    @field_validator("requested_amount", mode="before")
    @classmethod
    def _amt(cls, v: Any) -> Decimal:
        parsed = _parse_decimal(v, "requested_amount")
        if parsed is None or parsed <= 0:
            raise ValueError("requested_amount: must be a positive number")
        return parsed

    @field_validator("allows_partial_payment", mode="before")
    @classmethod
    def _b(cls, v: Any) -> bool:
        return _parse_bool(v, "allows_partial_payment")

    @model_validator(mode="after")
    def _checks(self) -> "Request":
        if self.desired_completion_date < self.request_date:
            raise ValueError("desired_completion_date: must be on or after request_date")
        return self


class SampleRequest(Request):
    """A request row that also carries the organizer-provided solved output columns."""

    amount_safe_to_pay: Decimal
    affordability_status: AffordabilityStatus
    recommended_payment_method: OutputMethod
    payment_plan: str
    earliest_date_for_full_payment: Optional[date] = None
    spending_changes_needed: str
    decision_explanation: str

    @field_validator("amount_safe_to_pay", mode="before")
    @classmethod
    def _safe(cls, v: Any) -> Decimal:
        parsed = _parse_decimal(v, "amount_safe_to_pay")
        if parsed is None or parsed < 0:
            raise ValueError("amount_safe_to_pay: must be a non-negative number")
        return parsed

    @field_validator("earliest_date_for_full_payment", mode="before")
    @classmethod
    def _earliest(cls, v: Any) -> Optional[date]:
        return _parse_date(v, "earliest_date_for_full_payment")

    @model_validator(mode="after")
    def _sample_checks(self) -> "SampleRequest":
        if self.amount_safe_to_pay > self.requested_amount:
            raise ValueError("amount_safe_to_pay: must be <= requested_amount")
        return self


# --------------------------------------------------------------------------------------
# request_payment_options.csv
# --------------------------------------------------------------------------------------


class PaymentOption(_Row):
    payment_option_id: str
    request_id: str
    payment_method: PaymentMethod
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int] = None
    financing_fee: Decimal
    total_payable_amount: Decimal

    @field_validator("payment_amount", "financing_fee", "total_payable_amount", mode="before")
    @classmethod
    def _money(cls, v: Any, info: Any) -> Decimal:
        parsed = _parse_decimal(v, info.field_name)
        if parsed is None:
            raise ValueError(f"{info.field_name}: required")
        if parsed < 0:
            raise ValueError(f"{info.field_name}: must be >= 0")
        return parsed

    @field_validator("first_payment_date", mode="before")
    @classmethod
    def _d(cls, v: Any) -> date:
        parsed = _parse_date(v, "first_payment_date")
        if parsed is None:
            raise ValueError("first_payment_date: required")
        return parsed

    @field_validator("number_of_payments", mode="before")
    @classmethod
    def _n(cls, v: Any) -> int:
        n = int(str(v).strip())
        if n < 1:
            raise ValueError("number_of_payments: must be >= 1")
        return n

    @field_validator("payment_frequency_days", mode="before")
    @classmethod
    def _freq(cls, v: Any) -> Optional[int]:
        v = _blank_to_none(v)
        if v is None:
            return None
        n = int(str(v).strip())
        if n < 1:
            raise ValueError("payment_frequency_days: must be >= 1 when present")
        return n

    @model_validator(mode="after")
    def _checks(self) -> "PaymentOption":
        if self.payment_method is PaymentMethod.PARTIAL_PAYMENT:
            raise ValueError("payment_method: seller options cannot be partial_payment")
        if self.number_of_payments > 1 and self.payment_frequency_days is None:
            raise ValueError("payment_frequency_days: required when number_of_payments > 1")
        if self.payment_method is PaymentMethod.FULL_PAYMENT and self.number_of_payments != 1:
            raise ValueError("full_payment options must have exactly one payment")
        if self.payment_method is PaymentMethod.INSTALLMENTS and self.number_of_payments < 2:
            raise ValueError("installments options must have at least two payments")
        return self

    @property
    def schedule(self) -> tuple[date, ...]:
        """Payment dates exactly as the seller defines them."""
        from datetime import timedelta

        step = self.payment_frequency_days or 0
        return tuple(
            self.first_payment_date + timedelta(days=step * k) for k in range(self.number_of_payments)
        )

    @property
    def last_payment_date(self) -> date:
        return self.schedule[-1]


# --------------------------------------------------------------------------------------
# messages.csv and images.csv
# --------------------------------------------------------------------------------------


class Message(_Row):
    message_id: str
    user_id: str
    request_id: Optional[str] = None
    related_event_id: Optional[str] = None
    sent_at: datetime
    source_type: MessageSource
    message_text: str

    @field_validator("request_id", "related_event_id", mode="before")
    @classmethod
    def _opt(cls, v: Any) -> Optional[str]:
        return _blank_to_none(v)

    @field_validator("sent_at", mode="before")
    @classmethod
    def _dt(cls, v: Any) -> datetime:
        text = str(v).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"sent_at: not an ISO-8601 timestamp: {v!r}") from exc

    @field_validator("message_text")
    @classmethod
    def _txt(cls, v: str) -> str:
        if not v:
            raise ValueError("message_text: required")
        return v


class ImageRef(_Row):
    image_id: str
    user_id: str
    request_id: Optional[str] = None
    related_event_id: Optional[str] = None

    @field_validator("request_id", "related_event_id", mode="before")
    @classmethod
    def _opt(cls, v: Any) -> Optional[str]:
        return _blank_to_none(v)

    @property
    def filename(self) -> str:
        return f"{self.image_id}.png"


# --------------------------------------------------------------------------------------
# output.csv (template / final)
# --------------------------------------------------------------------------------------

OUTPUT_COLUMNS: tuple[str, ...] = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)
