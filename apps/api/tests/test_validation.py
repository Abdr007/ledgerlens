"""Deterministic validation math (spec §8 Definition of Done, item c).

These assert the arithmetic itself, not that a function was called. Every
expectation below is hand-computed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.schemas import InvoiceExtraction, parse_date, parse_money
from app.pipeline.validate import (
    RULE_DATE_SANITY,
    RULE_DUE_AFTER_ISSUE,
    RULE_LINE_ITEM_MATH,
    RULE_LINE_ITEMS_SUM,
    RULE_TOTAL_POSITIVE,
    RULE_TOTALS_RECONCILE,
    RULE_VAT_RATE,
    RULE_VENDOR_PRESENT,
    validate_extraction,
)

TODAY = date(2026, 7, 29)

CLEAN = {
    "vendor": "Gulf Metals LLC",
    "invoice_number": "INV-1001",
    "issue_date": "2026-07-01",
    "due_date": "2026-07-31",
    "currency": "AED",
    "payment_terms": "Net 30",
    "line_items": [
        {"description": "Steel", "qty": "10", "unit_price": "100.00", "amount": "1000.00"},
        {"description": "Bolts", "qty": "5", "unit_price": "20.00", "amount": "100.00"},
    ],
    "subtotal": "1100.00",
    "tax": "55.00",
    "total": "1155.00",
}


def _validate(**overrides: object):
    payload = {**CLEAN, **overrides}
    return validate_extraction(InvoiceExtraction.model_validate(payload), today=TODAY)


def _failed_rules(report) -> set[str]:
    return {check.rule for check in report.checks if not check.passed}


def test_clean_invoice_passes_every_rule() -> None:
    report = _validate()
    assert report.passed
    assert _failed_rules(report) == set()


def test_subtotal_plus_tax_must_equal_total() -> None:
    # 1100.00 + 55.00 = 1155.00, so a printed 1200.00 is a 45.00 discrepancy.
    report = _validate(total="1200.00")
    assert not report.passed
    assert RULE_TOTALS_RECONCILE in _failed_rules(report)
    assert "45.00" in report.failure_summary()


def test_two_cent_rounding_is_tolerated() -> None:
    """Display rounding must not flood the review queue."""
    assert _validate(total="1155.02").passed
    assert not _validate(total="1155.05").passed


def test_line_items_must_sum_to_subtotal() -> None:
    report = _validate(subtotal="1500.00", tax="75.00", total="1575.00")
    assert RULE_LINE_ITEMS_SUM in _failed_rules(report)


def test_line_item_must_multiply_out() -> None:
    report = _validate(
        line_items=[
            {"description": "Steel", "qty": "10", "unit_price": "100.00", "amount": "999.00"},
            {"description": "Bolts", "qty": "5", "unit_price": "20.00", "amount": "100.00"},
        ],
        subtotal="1099.00",
        tax="54.95",
        total="1153.95",
    )
    assert RULE_LINE_ITEM_MATH in _failed_rules(report)


def test_uae_vat_enforced_only_for_aed() -> None:
    """5% is the UAE rate; enforcing it on a USD invoice would flag every foreign
    supplier, so the rule is currency-scoped."""
    aed = _validate(tax="200.00", total="1300.00")
    assert RULE_VAT_RATE in _failed_rules(aed)

    usd = _validate(currency="USD", tax="88.00", total="1188.00")
    assert usd.passed


def test_zero_rated_supply_is_legitimate() -> None:
    assert _validate(tax="0", total="1100.00").passed


def test_future_issue_date_is_rejected() -> None:
    report = _validate(issue_date="2027-01-01", due_date="2027-01-31")
    assert RULE_DATE_SANITY in _failed_rules(report)


def test_due_date_cannot_precede_issue_date() -> None:
    report = _validate(due_date="2026-06-01")
    assert RULE_DUE_AFTER_ISSUE in _failed_rules(report)


def test_missing_required_fields_block_auto_commit() -> None:
    report = _validate(vendor=None)
    assert not report.passed
    assert RULE_VENDOR_PRESENT in _failed_rules(report)


def test_negative_total_is_rejected() -> None:
    report = _validate(subtotal="-1100.00", tax="-55.00", total="-1155.00")
    assert RULE_TOTAL_POSITIVE in _failed_rules(report)


def test_failure_summary_names_the_rule_and_the_numbers() -> None:
    """The summary is fed back to the model as repair feedback, so it has to be
    specific enough to act on."""
    summary = _validate(total="1200.00").failure_summary()
    assert RULE_TOTALS_RECONCILE in summary
    assert "1155.00" in summary and "1200.00" in summary


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AED 1,234.56", Decimal("1234.56")),
        ("1.234,56", Decimal("1234.56")),
        ("(500.00)", Decimal("-500.00")),
        ("$99", Decimal("99")),
        ("1,23", Decimal("1.23")),
        ("12,345,678.90", Decimal("12345678.90")),
        ("  42  ", Decimal("42")),
        ("", None),
        ("abc", None),
        (None, None),
        (True, None),  # bool is an int subclass but never a money value
    ],
)
def test_money_parsing(raw: object, expected: Decimal | None) -> None:
    assert parse_money(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-14", date(2026, 3, 14)),
        ("14/03/2026", date(2026, 3, 14)),
        ("14 Mar 2026", date(2026, 3, 14)),
        ("March 14, 2026", date(2026, 3, 14)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_date_parsing(raw: object, expected: date | None) -> None:
    assert parse_date(raw) == expected


def test_schema_forbids_invented_fields() -> None:
    """`extra="forbid"` is what turns "the model made something up" into a
    repairable schema error instead of silent data."""
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        InvoiceExtraction.model_validate({**CLEAN, "approved_by": "the model"})


def test_nulls_are_allowed_everywhere() -> None:
    """A field that is genuinely absent must have a representation, or the model
    is cornered into guessing."""
    extraction = InvoiceExtraction.model_validate({})
    assert extraction.vendor is None
    assert extraction.total is None
    assert extraction.line_items == []
