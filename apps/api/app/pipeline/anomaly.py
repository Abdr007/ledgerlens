"""Explainable anomaly engine (spec §2, stage 6).

*"Fuzzy duplicate detection (vendor+amount+date window, rapidfuzz), per-vendor
amount z-scores, term drift, round-number bias. Every flag carries severity + a
plain-English reason."*

Four detectors, all explainable by construction — no model, no black box. Each
finding names the numbers that triggered it, so a finance team can agree or
disagree with the evidence rather than trusting a score.

`pandas` does the per-vendor statistics; `rapidfuzz` does the vendor-name
matching that makes "GULF METALS L.L.C." and "Gulf Metals LLC" the same supplier.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

import pandas as pd
from rapidfuzz import fuzz, utils

from app.models.enums import AnomalySeverity, AnomalyType
from app.models.schemas import AnomalyFinding

# A vendor needs at least this many priors before a z-score means anything.
_ZSCORE_HIGH_THRESHOLD: Final = 3.0
# Floor on a vendor's dispersion, as a fraction of their mean invoice value.
_MIN_RELATIVE_DISPERSION: Final = 0.01
_TERM_DRIFT_MIN_HISTORY: Final = 3
_TERM_DRIFT_MIN_CONSISTENCY: Final = 0.7
_ROUND_NUMBER_MIN_TOTAL: Final = Decimal("500")
_ROUND_STRONG_MULTIPLE: Final = Decimal("1000")
_ROUND_WEAK_MULTIPLE: Final = Decimal("500")

_LEGAL_SUFFIXES: Final = re.compile(
    r"\b(l\.?l\.?c|ltd|limited|inc|incorporated|co|company|corp|corporation|"
    r"fz[- ]?llc|fzco|fze|dmcc|plc|gmbh|sarl|pvt|private)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM: Final = re.compile(r"[^a-z0-9 ]+")


def normalise_vendor(name: str | None) -> str:
    """Canonical vendor key: lowercase, punctuation and legal suffixes removed.

    "GULF METALS L.L.C." and "Gulf Metals LLC" must land on the same key, or a
    duplicate invoice slips through simply because someone typed the suffix
    differently.
    """
    if not name:
        return ""
    lowered = name.strip().lower()
    without_suffix = _LEGAL_SUFFIXES.sub(" ", lowered)
    cleaned = _NON_ALNUM.sub(" ", without_suffix)
    return " ".join(cleaned.split())


def normalise_terms(terms: str | None) -> str:
    """Canonical payment-term key: "NET30", "Net 30", "net-30" -> "net 30"."""
    if not terms:
        return ""
    lowered = terms.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    lowered = re.sub(r"\bnet\s*(\d+)\b", r"net \1", lowered)
    return " ".join(lowered.split())


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    """A vendor-history row, as loaded from `extractions`."""

    document_id: uuid.UUID
    vendor: str | None
    invoice_number: str | None
    issue_date: date | None
    total: Decimal | None
    currency: str | None
    payment_terms: str | None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ScreeningConfig:
    """Thresholds, surfaced in settings so they are tunable without a code change."""

    vendor_similarity: int = 88
    amount_tolerance_pct: float = 0.01
    date_window_days: int = 7
    zscore_threshold: float = 2.0
    min_history_for_zscore: int = 4


def _relative_gap(a: Decimal, b: Decimal) -> Decimal:
    """|a - b| / max(|a|, |b|) — symmetric, so neither ordering biases the result."""
    scale = max(abs(a), abs(b))
    if scale == 0:
        return Decimal("0")
    return abs(a - b) / scale


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _detect_duplicates(
    candidate: InvoiceRecord, history: list[InvoiceRecord], config: ScreeningConfig
) -> list[AnomalyFinding]:
    """Vendor similarity + amount within tolerance + date within the window."""
    if candidate.total is None or candidate.issue_date is None or not candidate.vendor:
        return []

    findings: list[AnomalyFinding] = []
    candidate_key = normalise_vendor(candidate.vendor)

    for prior in history:
        if prior.document_id == candidate.document_id:
            continue
        if prior.total is None or prior.issue_date is None or not prior.vendor:
            continue
        if prior.currency and candidate.currency and prior.currency != candidate.currency:
            continue  # different currencies are not the same payment

        similarity = fuzz.token_set_ratio(
            candidate_key, normalise_vendor(prior.vendor), processor=utils.default_process
        )
        if similarity < config.vendor_similarity:
            continue

        gap_pct = _relative_gap(candidate.total, prior.total)
        if gap_pct > Decimal(str(config.amount_tolerance_pct)):
            continue

        day_gap = abs((candidate.issue_date - prior.issue_date).days)
        if day_gap > config.date_window_days:
            continue

        same_number = (
            candidate.invoice_number is not None
            and prior.invoice_number is not None
            and candidate.invoice_number.strip().lower() == prior.invoice_number.strip().lower()
        )
        number_clause = (
            f"under the same invoice number {candidate.invoice_number}"
            if same_number
            else (
                f"under a different invoice number "
                f"({candidate.invoice_number or 'none'} vs {prior.invoice_number or 'none'})"
            )
        )
        reason = (
            f"Possible duplicate payment: {candidate.vendor} was already billed "
            f"{_money(prior.total)} {prior.currency or ''} on "
            f"{prior.issue_date.isoformat()}, and this invoice bills "
            f"{_money(candidate.total)} on {candidate.issue_date.isoformat()} — "
            f"{gap_pct * 100:.2f}% apart, {day_gap} day(s) later, {number_clause}."
        ).replace("  ", " ")

        findings.append(
            AnomalyFinding(
                anomaly_type=AnomalyType.DUPLICATE,
                severity=AnomalySeverity.HIGH,
                reason=reason,
                score=float(similarity),
                evidence={
                    "matched_document_id": str(prior.document_id),
                    "matched_filename": prior.filename,
                    "matched_invoice_number": prior.invoice_number,
                    "matched_total": float(prior.total),
                    "matched_issue_date": prior.issue_date.isoformat(),
                    "candidate_total": float(candidate.total),
                    "candidate_issue_date": candidate.issue_date.isoformat(),
                    "vendor_similarity": float(similarity),
                    "amount_gap_pct": float(gap_pct * 100),
                    "day_gap": day_gap,
                    "same_invoice_number": same_number,
                },
                fingerprint=f"duplicate:{prior.document_id}",
            )
        )

    return findings


def _detect_amount_zscore(
    candidate: InvoiceRecord, history: list[InvoiceRecord], config: ScreeningConfig
) -> list[AnomalyFinding]:
    """Per-vendor amount z-score, computed with pandas over that vendor's priors."""
    if candidate.total is None or not candidate.vendor:
        return []

    candidate_key = normalise_vendor(candidate.vendor)
    peers = [
        prior
        for prior in history
        if prior.document_id != candidate.document_id
        and prior.total is not None
        and normalise_vendor(prior.vendor) == candidate_key
    ]
    if len(peers) < config.min_history_for_zscore:
        return []

    amounts = pd.Series([float(p.total) for p in peers if p.total is not None], dtype="float64")
    mean = float(amounts.mean())
    # Sample standard deviation (ddof=1): we are describing a sample of this
    # vendor's invoices, not an exhaustive population.
    observed_std = float(amounts.std(ddof=1))
    if pd.isna(observed_std) or mean == 0.0:
        return []

    # A vendor who bills an almost identical amount every month has a near-zero
    # standard deviation, and a raw z-score against it explodes: a 0.5% variation
    # would read as "50 sigma" and flood the queue with noise. Flooring the
    # dispersion at 1% of the mean keeps the statistic meaningful for stable
    # vendors while leaving genuinely volatile ones untouched.
    floor = abs(mean) * _MIN_RELATIVE_DISPERSION
    std = max(observed_std, floor)
    if std <= 0.0:
        return []

    value = float(candidate.total)
    zscore = (value - mean) / std
    if abs(zscore) <= config.zscore_threshold:
        return []

    severity = (
        AnomalySeverity.HIGH if abs(zscore) >= _ZSCORE_HIGH_THRESHOLD else AnomalySeverity.MEDIUM
    )
    direction = "higher" if zscore > 0 else "lower"
    multiple = value / mean if mean else 0.0
    floored = observed_std < floor

    # When the floor was applied the sigma count is an artefact of a vendor who
    # bills a near-identical amount every time, so quoting it would be misleading.
    # Explain the finding in the terms that are actually meaningful.
    if floored:
        reason = (
            f"Amount is {multiple:.1f}x this vendor's usual invoice: "
            f"{candidate.vendor} normally bills close to {mean:,.2f} "
            f"{candidate.currency or ''} (the last {len(peers)} invoices vary by under "
            f"1%), but this one is {_money(candidate.total)}."
        ).replace("  ", " ")
    else:
        reason = (
            f"Amount is {abs(zscore):.1f} standard deviations {direction} than normal for "
            f"{candidate.vendor}: this invoice is {_money(candidate.total)} "
            f"{candidate.currency or ''} against an average of {mean:,.2f} across "
            f"{len(peers)} prior invoices ({multiple:.1f}x the usual)."
        ).replace("  ", " ")

    return [
        AnomalyFinding(
            anomaly_type=AnomalyType.AMOUNT_ZSCORE,
            severity=severity,
            reason=reason,
            score=round(zscore, 4),
            evidence={
                "vendor_key": candidate_key,
                "candidate_total": value,
                "history_count": len(peers),
                "history_mean": round(mean, 2),
                "history_std": round(std, 2),
                "history_std_observed": round(observed_std, 2),
                "dispersion_floored": floored,
                "history_min": round(float(amounts.min()), 2),
                "history_max": round(float(amounts.max()), 2),
                "zscore": round(zscore, 4),
                "threshold": config.zscore_threshold,
            },
            fingerprint=f"amount_zscore:{candidate_key}",
        )
    ]


def _detect_term_drift(
    candidate: InvoiceRecord, history: list[InvoiceRecord], _config: ScreeningConfig
) -> list[AnomalyFinding]:
    """Payment terms that differ from what this vendor has consistently used.

    Silently shortened terms are a classic cash-flow attack and a common symptom
    of an intercepted invoice, so a vendor with a stable history that suddenly
    changes terms is worth a human glance.
    """
    if not candidate.vendor or not candidate.payment_terms:
        return []

    candidate_key = normalise_vendor(candidate.vendor)
    prior_terms = [
        normalise_terms(prior.payment_terms)
        for prior in history
        if prior.document_id != candidate.document_id
        and normalise_vendor(prior.vendor) == candidate_key
        and prior.payment_terms
    ]
    if len(prior_terms) < _TERM_DRIFT_MIN_HISTORY:
        return []

    counts = pd.Series(prior_terms, dtype="object").value_counts()
    modal_term = str(counts.index[0])
    consistency = float(counts.iloc[0]) / float(len(prior_terms))
    if consistency < _TERM_DRIFT_MIN_CONSISTENCY:
        return []  # this vendor has no settled convention to drift from

    candidate_term = normalise_terms(candidate.payment_terms)
    if candidate_term == modal_term:
        return []

    reason = (
        f"Payment terms changed for {candidate.vendor}: this invoice says "
        f'"{candidate.payment_terms}", but {counts.iloc[0]} of the last '
        f"{len(prior_terms)} invoices from this vendor used "
        f'"{modal_term}".'
    )

    return [
        AnomalyFinding(
            anomaly_type=AnomalyType.TERM_DRIFT,
            severity=AnomalySeverity.MEDIUM,
            reason=reason,
            score=round(consistency, 4),
            evidence={
                "vendor_key": candidate_key,
                "candidate_terms": candidate.payment_terms,
                "modal_terms": modal_term,
                "modal_count": int(counts.iloc[0]),
                "history_count": len(prior_terms),
                "consistency": round(consistency, 4),
            },
            fingerprint=f"term_drift:{candidate_key}",
        )
    ]


def _detect_round_number(
    candidate: InvoiceRecord, _history: list[InvoiceRecord], _config: ScreeningConfig
) -> list[AnomalyFinding]:
    """Suspiciously round totals.

    A genuine itemised invoice with tax almost never lands on an exact multiple of
    1000 — round totals correlate with estimates, placeholders and fabricated
    invoices. Low severity on its own; it earns its keep in combination.
    """
    total = candidate.total
    if total is None or total < _ROUND_NUMBER_MIN_TOTAL:
        return []

    if total % _ROUND_STRONG_MULTIPLE == 0:
        severity, multiple = AnomalySeverity.MEDIUM, _ROUND_STRONG_MULTIPLE
    elif total % _ROUND_WEAK_MULTIPLE == 0:
        severity, multiple = AnomalySeverity.LOW, _ROUND_WEAK_MULTIPLE
    else:
        return []

    reason = (
        f"Total is an exactly round {_money(total)} {candidate.currency or ''} — a whole "
        f"multiple of {int(multiple):,}. Itemised invoices with tax rarely land on a "
        f"round figure, which is typical of an estimate or a placeholder amount."
    ).replace("  ", " ")

    return [
        AnomalyFinding(
            anomaly_type=AnomalyType.ROUND_NUMBER,
            severity=severity,
            reason=reason,
            score=float(multiple),
            evidence={
                "total": float(total),
                "multiple_of": int(multiple),
                "currency": candidate.currency,
            },
            fingerprint=f"round_number:{int(multiple)}",
        )
    ]


def screen_invoice(
    candidate: InvoiceRecord,
    history: list[InvoiceRecord],
    config: ScreeningConfig | None = None,
) -> list[AnomalyFinding]:
    """Run all four detectors and return findings, most severe first."""
    active = config or ScreeningConfig()
    findings: list[AnomalyFinding] = []
    findings.extend(_detect_duplicates(candidate, history, active))
    findings.extend(_detect_amount_zscore(candidate, history, active))
    findings.extend(_detect_term_drift(candidate, history, active))
    findings.extend(_detect_round_number(candidate, history, active))
    findings.sort(key=lambda finding: (-finding.severity.rank, finding.anomaly_type.value))
    return findings
