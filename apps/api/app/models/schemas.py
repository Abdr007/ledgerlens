"""Domain schemas — the contract the model is forced to satisfy.

`InvoiceExtraction` is the strict Pydantic v2 schema from spec §8. It is the
*single* definition of what a valid extraction is: the same class produces the
Claude tool schema, parses the model's tool call, and is what the deterministic
validator runs against.

Two rules from the spec are encoded structurally rather than left to the prompt:

* **Nulls are allowed** — every field is optional, so "not present on the page"
  has a first-class representation and the model is never cornered into guessing.
* **Invention is forbidden** — `extra="forbid"` rejects any field the model made
  up, and a rejected payload is fed back into the self-correction loop.
"""

from __future__ import annotations

import re
from datetime import UTC, date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AnomalySeverity, AnomalyType, DocumentKind, Lane

_CURRENCY_NOISE: Final = re.compile(r"[^\d.,()\-]")
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
)
_MAX_LINE_ITEMS: Final = 200


def parse_money(value: Any) -> Decimal | None:
    """Normalise a monetary value emitted by the model into a `Decimal`.

    Handles currency symbols, thousands separators, both decimal conventions and
    accounting-style negatives — `"AED 1,234.56"`, `"1.234,56"`, `"(500.00)"`.
    This is *normalisation*, never invention: unparseable input returns `None`
    so the validator can flag it rather than a wrong number entering the ledger.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; never a money value
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    cleaned = _CURRENCY_NOISE.sub("", text).replace("(", "").replace(")", "")
    if not cleaned or cleaned in {"-", ".", ","}:
        return None

    if "," in cleaned and "." in cleaned:
        # Whichever separator appears last is the decimal point.
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in cleaned:
        tail = cleaned.rsplit(",", 1)[1]
        # "1,234" is thousands; "1,23" is a decimal comma.
        cleaned = cleaned.replace(",", "") if len(tail) == 3 else cleaned.replace(",", ".")

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -amount if negative and amount > 0 else amount


def parse_date(value: Any) -> date | None:
    """Normalise a date emitted by the model. Unparseable input becomes `None`."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-"}:
        return None
    if _ISO_DATE.match(text):
        from datetime import datetime as _dt

        try:
            # Anchored to UTC purely to keep the value tz-aware; a calendar date
            # has no instant semantics, so no shift is possible.
            return _dt.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC).date()
        except ValueError:
            return None
    from datetime import datetime as _dt

    for fmt in _DATE_FORMATS:
        try:
            return _dt.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


class LineItem(BaseModel):
    """A single billed line. Field names are fixed by spec §8."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=512)
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None

    @field_validator("qty", "unit_price", "amount", mode="before")
    @classmethod
    def _coerce_numeric(cls, value: Any) -> Any:
        return parse_money(value)

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> Any:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text[:512] or None


class InvoiceExtraction(BaseModel):
    """The strict extraction schema. Every field optional; unknown fields rejected."""

    model_config = ConfigDict(extra="forbid")

    vendor: str | None = Field(default=None, max_length=256)
    invoice_number: str | None = Field(default=None, max_length=128)
    issue_date: date | None = None
    due_date: date | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = Field(default=None, max_length=8)
    payment_terms: str | None = Field(default=None, max_length=128)

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _coerce_money(cls, value: Any) -> Any:
        return parse_money(value)

    @field_validator("issue_date", "due_date", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        return parse_date(value)

    @field_validator("vendor", "invoice_number", "payment_terms", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text or None

    @field_validator("currency", mode="before")
    @classmethod
    def _clean_currency(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip().upper()
        symbols = {"$": "USD", "€": "EUR", "£": "GBP", "AED": "AED", "DHS": "AED", "₹": "INR"}
        if text in symbols:
            return symbols[text]
        return text[:8] or None

    @field_validator("line_items", mode="before")
    @classmethod
    def _bound_line_items(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list):
            return value[:_MAX_LINE_ITEMS]
        return value


class RoutingDecision(BaseModel):
    """Output of the Haiku routing gate (spec §2, stage 2)."""

    model_config = ConfigDict(extra="forbid")

    doc_kind: DocumentKind = DocumentKind.UNKNOWN
    lane: Lane = Lane.VISION
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=512)

    @field_validator("doc_kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: Any) -> Any:
        if value is None:
            return DocumentKind.UNKNOWN
        text = str(value).strip().lower()
        return text if text in set(DocumentKind) else DocumentKind.UNKNOWN

    @field_validator("lane", mode="before")
    @classmethod
    def _coerce_lane(cls, value: Any) -> Any:
        if value is None:
            return Lane.VISION
        text = str(value).strip().lower()
        # The model is asked for the document's nature; map it onto a lane.
        if text in {"digital", "digital_text", "text", "native"}:
            return Lane.TEXT
        if text in {"scanned", "scan", "photo", "image", "vision"}:
            return Lane.VISION
        return text if text in set(Lane) else Lane.VISION

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> Any:
        if value is None:
            return 0.0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return min(max(number, 0.0), 1.0)


class ValidationCheck(BaseModel):
    """One deterministic rule result."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    passed: bool
    message: str
    expected: str | None = None
    observed: str | None = None


class ValidationReport(BaseModel):
    """Outcome of the pure-Python validation layer (spec §2, stage 5)."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    checks: list[ValidationCheck] = Field(default_factory=list)

    @property
    def failures(self) -> list[ValidationCheck]:
        return [check for check in self.checks if not check.passed]

    def failure_summary(self) -> str:
        """Compact, model-readable description of what went wrong.

        This string is what gets fed back into the self-correction retry, so it
        names the rule and the numbers rather than saying "validation failed".
        """
        return "; ".join(f"{c.rule}: {c.message}" for c in self.failures) or "all checks passed"


class AnomalyFinding(BaseModel):
    """One explainable flag produced by the anomaly engine."""

    model_config = ConfigDict(extra="forbid")

    anomaly_type: AnomalyType
    severity: AnomalySeverity
    reason: str
    score: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str
