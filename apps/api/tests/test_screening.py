"""Screening: how much history is loaded, and how a duplicate is explained.

Both cases here are regressions with the same shape — the detectors were right and
the code around them was wrong. The history query fetched the whole ledger on every
upload, and the duplicate sentence described every match as "later" regardless of
which invoice actually came first.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.settings import Settings, get_settings
from app.models.enums import AnomalyType
from app.pipeline.anomaly import InvoiceRecord, normalise_vendor, screen_invoice
from app.pipeline.orchestrator import _history_predicate


def _record(
    *,
    vendor: str | None = "Gulf Metals LLC",
    issue_date: date | None = date(2026, 3, 10),
    total: Decimal | None = Decimal("10000.00"),
    invoice_number: str | None = "INV-1",
    currency: str | None = "AED",
) -> InvoiceRecord:
    return InvoiceRecord(
        document_id=uuid.uuid4(),
        vendor=vendor,
        invoice_number=invoice_number,
        issue_date=issue_date,
        total=total,
        currency=currency,
        payment_terms="Net 30",
        filename="invoice.pdf",
    )


def _sql(candidate: InvoiceRecord, settings: Settings) -> str:
    return str(
        _history_predicate(candidate, settings).compile(compile_kwargs={"literal_binds": True})
    )


# --- F-1: the history query is bounded -------------------------------------


def test_candidate_without_vendor_or_date_fetches_no_history() -> None:
    """Every history-consuming detector returns early here, so fetch nothing."""
    predicate = _sql(_record(vendor=None, issue_date=None), get_settings())
    assert "false" in predicate.lower()


def test_predicate_bounds_on_both_vendor_and_date_window() -> None:
    settings = get_settings()
    predicate = _sql(_record(), settings).lower()

    # Vendor: the normalised key's first token, matched case-insensitively.
    assert "lower(extractions.vendor)" in predicate
    assert "gulf" in predicate

    # Date: exactly the duplicate detector's reach, in both directions.
    window = settings.duplicate_date_window_days
    assert "between" in predicate
    assert str(date(2026, 3, 10).replace(day=10 - window)) in predicate  # 2026-03-03
    assert "2026-03-17" in predicate


def test_vendor_clause_dropped_when_the_name_normalises_to_nothing() -> None:
    """A vendor that is only a legal suffix has no usable identity to group on."""
    assert normalise_vendor("LLC") == ""
    predicate = _sql(_record(vendor="LLC"), get_settings()).lower()
    assert "lower(extractions.vendor)" not in predicate
    assert "between" in predicate  # still reachable as a same-window duplicate


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("GULF METALS L.L.C.", "Gulf Metals LLC"),
        ("Gulf Metals Ltd.", "  gulf   metals  "),
        ("Al Noor Trading FZ-LLC", "AL NOOR TRADING (FZCO)"),
        ("Delta-Systems, Inc.", "Delta Systems Incorporated"),
    ],
)
def test_like_token_never_excludes_a_row_python_would_match(left: str, right: str) -> None:
    """The SQL clause must be a *superset* of the Python equality it prefilters.

    Normalisation only ever blanks characters, so every token of a key is a
    contiguous alphanumeric run of the lowercased raw name. If two names normalise
    to the same key, each therefore contains that key's first token — which is what
    makes it safe to narrow in SQL and still match exactly in Python.
    """
    assert normalise_vendor(left) == normalise_vendor(right)
    token = normalise_vendor(left).split(" ", 1)[0]
    assert token in left.lower()
    assert token in right.lower()


# --- F-4: the duplicate sentence states the right direction ----------------


def _duplicate_reason(candidate_day: int, prior_day: int) -> str:
    candidate = _record(issue_date=date(2026, 3, candidate_day), invoice_number="INV-2")
    prior = _record(issue_date=date(2026, 3, prior_day), invoice_number="INV-1")
    findings = [
        f for f in screen_invoice(candidate, [prior]) if f.anomaly_type is AnomalyType.DUPLICATE
    ]
    assert findings, "expected the planted duplicate to be detected"
    return findings[0].reason


def test_a_later_candidate_reads_as_later() -> None:
    assert "3 day(s) later" in _duplicate_reason(candidate_day=13, prior_day=10)


def test_a_backdated_candidate_reads_as_earlier() -> None:
    """History is not ordered by issue date — a backdated invoice can arrive last."""
    assert "3 day(s) earlier" in _duplicate_reason(candidate_day=10, prior_day=13)


def test_same_day_says_so() -> None:
    assert "the same day" in _duplicate_reason(candidate_day=10, prior_day=10)
