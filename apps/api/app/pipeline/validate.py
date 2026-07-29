"""Deterministic validation — pure Python, never an LLM (spec §2, stage 5).

*"LLMs extract, code verifies. The model never checks its own math — a
deterministic validation layer does, and anything that fails routes to a human
review queue instead of the database."*

Everything here is exact `Decimal` arithmetic with explicit, documented
tolerances. There is no model call, no randomness and no network: the same
extraction always produces the same verdict, which is what makes the result
defensible to a finance team.

Tolerances are derived, not guessed:

* `subtotal + tax == total` is arithmetic printed on the page, so a flat two-cent
  tolerance covers display rounding.
* A sum of *n* line items can accumulate up to half a cent of rounding per line,
  so its tolerance grows with *n* rather than staying flat.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from app.models.schemas import InvoiceExtraction, ValidationCheck, ValidationReport

# Rule identifiers are stable strings: they are stored in the DB, shown in the UI
# and fed back to the model during self-correction.
RULE_VENDOR_PRESENT: Final = "vendor_present"
RULE_INVOICE_NUMBER_PRESENT: Final = "invoice_number_present"
RULE_ISSUE_DATE_PRESENT: Final = "issue_date_present"
RULE_TOTAL_PRESENT: Final = "total_present"
RULE_CURRENCY_PRESENT: Final = "currency_present"
RULE_TOTAL_POSITIVE: Final = "total_positive"
RULE_LINE_ITEM_MATH: Final = "line_item_math"
RULE_LINE_ITEMS_SUM: Final = "line_items_sum_to_subtotal"
RULE_TOTALS_RECONCILE: Final = "subtotal_plus_tax_equals_total"
RULE_VAT_RATE: Final = "uae_vat_rate"
RULE_DATE_SANITY: Final = "date_sanity"
RULE_DUE_AFTER_ISSUE: Final = "due_date_after_issue_date"

_EARLIEST_PLAUSIBLE_DATE: Final = date(1990, 1, 1)
_MAX_FUTURE_SKEW: Final = timedelta(days=1)
_MAX_PAYMENT_WINDOW: Final = timedelta(days=1095)  # 3 years
_PER_LINE_ROUNDING: Final = Decimal("0.01")
_MAX_REPORTED_LINE_ERRORS: Final = 3
_UAE_CURRENCY: Final = "AED"


def _fmt(value: Decimal | None) -> str:
    return "null" if value is None else f"{value:.2f}"


def _within(actual: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    return abs(actual - expected) <= tolerance


def _presence_check(rule: str, label: str, value: object) -> ValidationCheck:
    present = value is not None and value != ""
    return ValidationCheck(
        rule=rule,
        passed=present,
        message=(
            f"{label} was extracted."
            if present
            else f"{label} is missing, so the document cannot be auto-committed."
        ),
        expected="a non-null value",
        observed="present" if present else "null",
    )


def _check_line_item_math(extraction: InvoiceExtraction) -> list[ValidationCheck]:
    """`qty * unit_price == amount` for every line that supplies all three."""
    mismatches: list[str] = []
    evaluated = 0
    for index, item in enumerate(extraction.line_items, start=1):
        if item.qty is None or item.unit_price is None or item.amount is None:
            continue
        evaluated += 1
        expected = (item.qty * item.unit_price).quantize(Decimal("0.01"))
        if not _within(item.amount, expected, _PER_LINE_ROUNDING):
            mismatches.append(
                f"line {index} ({item.description or 'unnamed'}): "
                f"{_fmt(item.qty)} x {_fmt(item.unit_price)} = {_fmt(expected)}, "
                f"but {_fmt(item.amount)} is printed"
            )

    if evaluated == 0:
        return []

    if not mismatches:
        return [
            ValidationCheck(
                rule=RULE_LINE_ITEM_MATH,
                passed=True,
                message=f"All {evaluated} priced line item(s) multiply out correctly.",
            )
        ]

    shown = mismatches[:_MAX_REPORTED_LINE_ERRORS]
    suffix = (
        ""
        if len(mismatches) <= _MAX_REPORTED_LINE_ERRORS
        else f" (+{len(mismatches) - _MAX_REPORTED_LINE_ERRORS} more)"
    )
    return [
        ValidationCheck(
            rule=RULE_LINE_ITEM_MATH,
            passed=False,
            message=f"{len(mismatches)} line item(s) do not multiply out: "
            + "; ".join(shown)
            + suffix,
            expected="qty x unit_price == amount",
            observed=f"{len(mismatches)} of {evaluated} lines mismatched",
        )
    ]


def _check_line_items_sum(extraction: InvoiceExtraction) -> list[ValidationCheck]:
    """Sum of line amounts reconciles to the printed subtotal."""
    amounts = [item.amount for item in extraction.line_items if item.amount is not None]
    if not amounts or extraction.subtotal is None:
        return []

    total_of_lines = sum(amounts, start=Decimal("0"))
    # Each line may be rounded to 2dp, so allowable drift grows with the line count.
    tolerance = _PER_LINE_ROUNDING * Decimal(len(amounts))
    passed = _within(total_of_lines, extraction.subtotal, tolerance)
    return [
        ValidationCheck(
            rule=RULE_LINE_ITEMS_SUM,
            passed=passed,
            message=(
                f"{len(amounts)} line item(s) sum to {_fmt(total_of_lines)}, matching the "
                f"printed subtotal."
                if passed
                else (
                    f"{len(amounts)} line item(s) sum to {_fmt(total_of_lines)} but the "
                    f"subtotal reads {_fmt(extraction.subtotal)} — a difference of "
                    f"{_fmt(abs(total_of_lines - extraction.subtotal))}."
                )
            ),
            expected=_fmt(extraction.subtotal),
            observed=_fmt(total_of_lines),
        )
    ]


def _check_totals_reconcile(
    extraction: InvoiceExtraction, tolerance: Decimal
) -> list[ValidationCheck]:
    """`subtotal + tax == total`."""
    if extraction.subtotal is None or extraction.total is None:
        return []
    tax = extraction.tax if extraction.tax is not None else Decimal("0")
    expected = extraction.subtotal + tax
    passed = _within(expected, extraction.total, tolerance)
    return [
        ValidationCheck(
            rule=RULE_TOTALS_RECONCILE,
            passed=passed,
            message=(
                f"Subtotal {_fmt(extraction.subtotal)} + tax {_fmt(tax)} equals the "
                f"printed total {_fmt(extraction.total)}."
                if passed
                else (
                    f"Subtotal {_fmt(extraction.subtotal)} + tax {_fmt(tax)} = "
                    f"{_fmt(expected)}, but the total reads {_fmt(extraction.total)} — "
                    f"a difference of {_fmt(abs(expected - extraction.total))}."
                )
            ),
            expected=_fmt(expected),
            observed=_fmt(extraction.total),
        )
    ]


def _check_vat_rate(
    extraction: InvoiceExtraction, vat_rate: float, tolerance: Decimal
) -> list[ValidationCheck]:
    """UAE VAT sanity — applied only where it is actually the governing rate.

    Enforcing 5% on a USD or EUR invoice would flag every non-UAE supplier, so the
    rule runs when the document is denominated in AED and charges a non-zero tax.
    Zero tax is left alone: zero-rated and exempt supplies are legitimate.
    """
    if extraction.currency != _UAE_CURRENCY:
        return []
    if extraction.subtotal is None or extraction.tax is None:
        return []
    if extraction.tax == 0:
        return []

    expected = (extraction.subtotal * Decimal(str(vat_rate))).quantize(Decimal("0.01"))
    # Allow the greater of the flat tolerance and one cent per 100 units of base.
    allowed = max(tolerance, (extraction.subtotal * Decimal("0.0002")).copy_abs())
    passed = _within(extraction.tax, expected, allowed)
    observed_rate = (
        (extraction.tax / extraction.subtotal * 100) if extraction.subtotal else Decimal("0")
    )
    return [
        ValidationCheck(
            rule=RULE_VAT_RATE,
            passed=passed,
            message=(
                f"Tax of {_fmt(extraction.tax)} AED is the expected {vat_rate:.0%} UAE VAT "
                f"on a subtotal of {_fmt(extraction.subtotal)}."
                if passed
                else (
                    f"Tax of {_fmt(extraction.tax)} AED is {observed_rate:.2f}% of the "
                    f"{_fmt(extraction.subtotal)} subtotal, not the expected "
                    f"{vat_rate:.0%} UAE VAT ({_fmt(expected)})."
                )
            ),
            expected=_fmt(expected),
            observed=_fmt(extraction.tax),
        )
    ]


def _check_dates(extraction: InvoiceExtraction, today: date) -> list[ValidationCheck]:
    """Issue date is plausible; a due date cannot precede issue."""
    checks: list[ValidationCheck] = []
    issue = extraction.issue_date
    due = extraction.due_date

    if issue is not None:
        latest_allowed = today + _MAX_FUTURE_SKEW
        in_range = _EARLIEST_PLAUSIBLE_DATE <= issue <= latest_allowed
        checks.append(
            ValidationCheck(
                rule=RULE_DATE_SANITY,
                passed=in_range,
                message=(
                    f"Issue date {issue.isoformat()} is plausible."
                    if in_range
                    else (
                        f"Issue date {issue.isoformat()} is outside the plausible range "
                        f"{_EARLIEST_PLAUSIBLE_DATE.isoformat()} to "
                        f"{latest_allowed.isoformat()}."
                    )
                ),
                expected=f"{_EARLIEST_PLAUSIBLE_DATE.isoformat()}..{latest_allowed.isoformat()}",
                observed=issue.isoformat(),
            )
        )

    if issue is not None and due is not None:
        ordered = due >= issue
        within_window = due <= issue + _MAX_PAYMENT_WINDOW
        passed = ordered and within_window
        if ordered and not within_window:
            message = (
                f"Due date {due.isoformat()} is more than three years after the issue "
                f"date {issue.isoformat()}."
            )
        elif not ordered:
            message = f"Due date {due.isoformat()} precedes the issue date {issue.isoformat()}."
        else:
            message = f"Due date {due.isoformat()} follows the issue date correctly."
        checks.append(
            ValidationCheck(
                rule=RULE_DUE_AFTER_ISSUE,
                passed=passed,
                message=message,
                expected=f">= {issue.isoformat()}",
                observed=due.isoformat(),
            )
        )

    return checks


def validate_extraction(
    extraction: InvoiceExtraction,
    *,
    vat_rate: float = 0.05,
    money_tolerance: float = 0.02,
    today: date | None = None,
) -> ValidationReport:
    """Run every deterministic rule. `passed` is the auto-commit gate.

    A failing report never blocks ingestion — it routes the document to
    `NEEDS_REVIEW` so a human sees it, which is the whole point of the layer.
    """
    reference_day = today or datetime.now(UTC).date()
    tolerance = Decimal(str(money_tolerance))

    checks: list[ValidationCheck] = [
        _presence_check(RULE_VENDOR_PRESENT, "Vendor", extraction.vendor),
        _presence_check(RULE_INVOICE_NUMBER_PRESENT, "Invoice number", extraction.invoice_number),
        _presence_check(RULE_ISSUE_DATE_PRESENT, "Issue date", extraction.issue_date),
        _presence_check(RULE_TOTAL_PRESENT, "Total", extraction.total),
        _presence_check(RULE_CURRENCY_PRESENT, "Currency", extraction.currency),
    ]

    if extraction.total is not None:
        positive = extraction.total > 0
        checks.append(
            ValidationCheck(
                rule=RULE_TOTAL_POSITIVE,
                passed=positive,
                message=(
                    f"Total {_fmt(extraction.total)} is a positive amount."
                    if positive
                    else f"Total {_fmt(extraction.total)} is not a positive amount."
                ),
                expected="> 0.00",
                observed=_fmt(extraction.total),
            )
        )

    checks.extend(_check_line_item_math(extraction))
    checks.extend(_check_line_items_sum(extraction))
    checks.extend(_check_totals_reconcile(extraction, tolerance))
    checks.extend(_check_vat_rate(extraction, vat_rate, tolerance))
    checks.extend(_check_dates(extraction, reference_day))

    return ValidationReport(passed=all(check.passed for check in checks), checks=checks)
