"""Deterministic offline handlers for `OfflineClaudeClient`.

Why this exists: the pipeline must be fully runnable — and testable in CI — with
no API key and no network. Rather than mocking the model away (which would leave
hashing, routing, validation, screening and persistence untested), these handlers
answer the same forced-tool contracts using a real rule-based parser over the
document's *actual* extracted text.

That makes the offline mode a legitimate baseline system in its own right: classic
labelled-field extraction, the thing vision-LLM extraction is supposed to beat.
`eval/run_eval.py` scores both, so the comparison is measured rather than asserted.

Honest limitation, surfaced rather than hidden: this parser reads **text**. A
photograph with no text layer yields nulls, the deterministic validator fails its
presence checks, and the document routes to `NEEDS_REVIEW` — the correct outcome
for "we could not read this without a vision model".
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final

from app.core.claude import LlmRequest
from app.models.enums import DocumentKind, Lane
from app.models.schemas import parse_money
from app.pipeline.prompts import DOCUMENT_CLOSE, DOCUMENT_OPEN

# A monetary token. The grouped-thousands form is tried first and *requires* its
# separator, so a bare "2400" is matched whole by the plain form instead of being
# chopped into "240" + "0" by a greedy three-digit group.
_NUMBER: Final = re.compile(
    r"\(?-?(?:"
    r"\d{1,3}(?:[,\x20\xa0]\d{3})+(?:\.\d{1,4})?"  # 1,234 / 1 234.56
    r"|\d+(?:\.\d{1,4})?"  # 2400 / 5040.00 / 0.00
    r")\)?"
)

# The label must carry an explicit marker ("No", "#", ":") and stay on one line,
# otherwise the bare word in a "TAX INVOICE" banner matches and the following
# word is captured as the reference. The reference itself must contain a digit.
_INVOICE_NUMBER: Final = re.compile(
    r"(?:invoice|inv|bill|document|reference|ref)[ \t]*"
    r"(?:no\.?|number|num|#|id)?[ \t]*[:#][ \t]*"
    r"(?=[A-Z0-9\-/_]*\d)([A-Z0-9][A-Z0-9\-/_]{2,30})",
    re.IGNORECASE,
)
_ISSUE_DATE: Final = re.compile(
    r"(?:invoice\s*date|issue\s*date|issued(?:\s*on)?|date\s*of\s*issue|^\s*date)\s*[:#]?\s*"
    r"([0-9]{1,4}[-/. ][A-Za-z0-9]{1,9}[-/. ][0-9]{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE | re.MULTILINE,
)
_DUE_DATE: Final = re.compile(
    r"(?:due\s*date|payment\s*due|due\s*by|pay\s*by)\s*[:#]?\s*"
    r"([0-9]{1,4}[-/. ][A-Za-z0-9]{1,9}[-/. ][0-9]{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_SUBTOTAL: Final = re.compile(
    r"(?:sub[\s-]*total|net\s*amount|amount\s*before\s*(?:tax|vat))\s*[:#]?\s*[^\d\-(]{0,12}"
    r"(\(?-?[\d,.  ]+\)?)",
    re.IGNORECASE,
)
_TAX: Final = re.compile(
    r"(?:vat|tax|gst)(?:\s*@?\s*\d{1,2}(?:\.\d+)?\s*%)?\s*[:#]?\s*[^\d\-(]{0,12}"
    r"(\(?-?[\d,.  ]+\)?)",
    re.IGNORECASE,
)
_TOTAL: Final = re.compile(
    r"(?:grand\s*total|total\s*due|amount\s*due|balance\s*due|total\s*payable|total)"
    r"\s*[:#]?\s*[^\d\-(]{0,12}(\(?-?[\d,.  ]+\)?)",
    re.IGNORECASE,
)
_TERMS: Final = re.compile(
    r"(?:payment\s*terms|terms\s*of\s*payment|terms)\s*[:#]?\s*([^\n]{2,60})", re.IGNORECASE
)
_CURRENCY_CODE: Final = re.compile(r"\b(AED|USD|EUR|GBP|INR|SAR|QAR|KWD|OMR|BHD)\b")
_CURRENCY_SYMBOL: Final = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "د.إ": "AED"}
_TRN: Final = re.compile(r"\b(?:trn|tax\s*reg|vat\s*reg|gstin)\b", re.IGNORECASE)

_HEADER_NOISE: Final = re.compile(
    r"^\s*(tax\s+invoice|invoice|receipt|credit\s*note|proforma|statement|bill)\s*$",
    re.IGNORECASE,
)
_TABLE_HEADER: Final = re.compile(
    r"(description|item|particulars).*(qty|quantity).*(price|rate|amount)", re.IGNORECASE
)
_STOP_WORDS: Final = re.compile(
    r"^\s*(sub[\s-]*total|total|vat|tax|grand\s*total|amount\s*due|balance|payment\s*terms|"
    r"bank|iban|swift|notes?|thank)",
    re.IGNORECASE,
)

_KIND_HINTS: Final[tuple[tuple[DocumentKind, tuple[str, ...]], ...]] = (
    (
        DocumentKind.CONTRACT,
        (
            "agreement",
            "this contract",
            "statement of work",
            "terms and conditions",
            "hereinafter",
            "party of the",
        ),
    ),
    (
        DocumentKind.RECEIPT,
        (
            "receipt",
            "paid in full",
            "payment received",
            "change due",
            "cash tendered",
            "thank you for your payment",
        ),
    ),
    (
        DocumentKind.INVOICE,
        ("invoice", "amount due", "bill to", "payment terms", "total due", "vat", "purchase order"),
    ),
)


def extract_document_block(text: str) -> str:
    """Return only the fenced untrusted-document region, if one is present."""
    start = text.find(DOCUMENT_OPEN)
    end = text.find(DOCUMENT_CLOSE)
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start + len(DOCUMENT_OPEN) : end].strip()


def _last_amount(pattern: re.Pattern[str], text: str) -> Decimal | None:
    """Totals live at the bottom of a document, so the last match wins."""
    matches = pattern.findall(text)
    for raw in reversed(matches):
        value = parse_money(raw)
        if value is not None:
            return value
    return None


_COLUMN_GAP: Final = re.compile(r"\s{2,}")


def _guess_vendor(lines: list[str]) -> str | None:
    """The issuing party is conventionally the first substantive line.

    Layout-preserved text keeps columns on one row, so the letterhead line also
    carries the right-aligned "TAX INVOICE" banner. Splitting on the column gap
    and taking the left-most cell recovers the vendor alone.
    """
    for raw in lines[:8]:
        line = _COLUMN_GAP.split(raw.strip())[0] if raw.strip() else raw
        stripped = line.strip()
        if len(stripped) < 3 or _HEADER_NOISE.match(stripped) or _TRN.search(stripped):
            continue
        if re.fullmatch(r"[\d\W_]+", stripped):
            continue
        if re.search(r"(invoice|date|page)\s*(no|#|:)", stripped, re.IGNORECASE):
            continue
        return stripped[:256]
    return None


def _guess_currency(text: str) -> str | None:
    code = _CURRENCY_CODE.search(text)
    if code:
        return code.group(1).upper()
    for symbol, iso in _CURRENCY_SYMBOL.items():
        if symbol in text:
            return iso
    return None


def _parse_line_items(lines: list[str]) -> list[dict[str, Any]]:
    """Parse the item table by reading numbers from the right.

    A billed row ends in `... qty unit_price amount`, so the rightmost numbers are
    unambiguous even when the description itself contains digits.
    """
    items: list[dict[str, Any]] = []
    in_table = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _TABLE_HEADER.search(line):
            in_table = True
            continue
        if _STOP_WORDS.match(line):
            if in_table:
                break
            continue
        if not in_table:
            continue

        matches = list(_NUMBER.finditer(line))
        if len(matches) < 3:
            continue

        amount = parse_money(matches[-1].group())
        unit_price = parse_money(matches[-2].group())
        qty = parse_money(matches[-3].group())
        if amount is None or unit_price is None or qty is None:
            continue

        # Cut at the *span* of the qty column, not a substring search: a
        # description like "Steel plate 10mm x 2400" can contain the same digits
        # as the quantity, and rfind would slice in the wrong place.
        description = " ".join(line[: matches[-3].start()].split()) or None
        items.append(
            {
                "description": description,
                "qty": str(qty),
                "unit_price": str(unit_price),
                "amount": str(amount),
            }
        )

    return items


def parse_invoice_text(text: str) -> dict[str, Any]:
    """Rule-based extraction into the `record_invoice_fields` shape."""
    if not text.strip():
        return {
            "vendor": None,
            "invoice_number": None,
            "issue_date": None,
            "due_date": None,
            "line_items": [],
            "subtotal": None,
            "tax": None,
            "total": None,
            "currency": None,
            "payment_terms": None,
        }

    lines = text.splitlines()

    invoice_number = None
    if (match := _INVOICE_NUMBER.search(text)) is not None:
        candidate = match.group(1).strip(" .:#")
        # Guard against swallowing a date or a bare year as a reference.
        if not re.fullmatch(r"\d{4}", candidate) and not re.fullmatch(r"[\d/.\-]{8,}", candidate):
            invoice_number = candidate

    issue_match = _ISSUE_DATE.search(text)
    due_match = _DUE_DATE.search(text)
    terms_match = _TERMS.search(text)

    subtotal = _last_amount(_SUBTOTAL, text)
    tax = _last_amount(_TAX, text)
    total = _last_amount(_TOTAL, text)

    # "Total" also matches inside "Subtotal"; if they collided, trust the subtotal.
    if total is not None and subtotal is not None and total == subtotal and tax:
        total = subtotal + tax

    return {
        "vendor": _guess_vendor(lines),
        "invoice_number": invoice_number,
        "issue_date": issue_match.group(1).strip() if issue_match else None,
        "due_date": due_match.group(1).strip() if due_match else None,
        "line_items": _parse_line_items(lines),
        "subtotal": str(subtotal) if subtotal is not None else None,
        "tax": str(tax) if tax is not None else None,
        "total": str(total) if total is not None else None,
        "currency": _guess_currency(text),
        # A layout-preserved row carries the next column too
        # ("Net 30      Due Date: ..."), so cut at the first column gap.
        "payment_terms": (
            _COLUMN_GAP.split(terms_match.group(1).strip())[0].strip(" .;") if terms_match else None
        ),
    }


def classify_text(text: str) -> DocumentKind:
    """Keyword-scored document-type classification."""
    lowered = text.lower()
    scores: dict[DocumentKind, int] = {}
    for kind, hints in _KIND_HINTS:
        scores[kind] = sum(1 for hint in hints if hint in lowered)
    best = max(scores, key=lambda kind: scores[kind])
    return best if scores[best] > 0 else DocumentKind.UNKNOWN


# ---------------------------------------------------------------------------
# Handlers registered on OfflineClaudeClient
# ---------------------------------------------------------------------------


def handle_classify(request: LlmRequest) -> dict[str, Any]:
    """Offline answer for the `classify_document` tool."""
    body = extract_document_block(request.user_text)
    kind = classify_text(body) if body else DocumentKind.UNKNOWN
    has_text = len(body) >= 60
    return {
        "doc_kind": str(kind),
        "lane": str(Lane.TEXT) if has_text else str(Lane.VISION),
        "confidence": 0.75 if body else 0.2,
        "reason": (
            f"Offline classifier: {len(body)} characters of machine-readable text, "
            f"keyword profile matched '{kind}'."
        ),
    }


def handle_extract(request: LlmRequest) -> dict[str, Any]:
    """Offline answer for the `record_invoice_fields` tool."""
    body = extract_document_block(request.user_text)
    return parse_invoice_text(body)


def build_offline_handlers() -> dict[str, Any]:
    """Tool-name -> handler registry for `OfflineClaudeClient`."""
    from app.pipeline.prompts import CLASSIFY_TOOL, EXTRACT_TOOL

    return {CLASSIFY_TOOL.name: handle_classify, EXTRACT_TOOL.name: handle_extract}
