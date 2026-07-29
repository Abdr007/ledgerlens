"""Read models: turn stored rows into the shapes the UI renders.

The pipeline visual is **event-sourced from the append-only audit log**, not from
a client-side timer. Spec §6 is explicit that the six stage nodes must be "driven
by real backend status polling, not faked timers", so a stage only turns green
because a row exists in `audit_log` saying it finished.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from app.models.api import (
    AnomalyOut,
    AuditEntryOut,
    DocumentSummary,
    ExtractionOut,
    StageProgress,
    TraceOut,
)
from app.models.enums import (
    PIPELINE_ORDER,
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    AuditEvent,
    DocumentKind,
    DocumentStatus,
    Lane,
    PipelineStage,
    StageState,
)
from app.models.schemas import ValidationCheck
from app.models.tables import Anomaly, AuditLog, Document, Extraction, LlmTrace

# Which audit event marks each stage finished, and with what outcome.
_STAGE_COMPLETION: Final[dict[AuditEvent, tuple[PipelineStage, StageState]]] = {
    AuditEvent.DOCUMENT_RECEIVED: (PipelineStage.INGEST, StageState.PASSED),
    AuditEvent.DOCUMENT_DEDUPLICATED: (PipelineStage.INGEST, StageState.PASSED),
    AuditEvent.ROUTED: (PipelineStage.ROUTE, StageState.PASSED),
    AuditEvent.EXTRACTION_SUCCEEDED: (PipelineStage.EXTRACT, StageState.PASSED),
    AuditEvent.VALIDATION_PASSED: (PipelineStage.VALIDATE, StageState.PASSED),
    AuditEvent.VALIDATION_FAILED: (PipelineStage.VALIDATE, StageState.FLAGGED),
    AuditEvent.SCREENING_COMPLETED: (PipelineStage.SCREEN, StageState.PASSED),
    AuditEvent.LEDGER_COMMITTED: (PipelineStage.LEDGER, StageState.PASSED),
}


def _as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _stage_detail(event: AuditEvent, payload: dict[str, object]) -> str | None:
    """Short human-readable caption shown under an active or finished node."""
    match event:
        case AuditEvent.DOCUMENT_RECEIVED:
            size = payload.get("size_bytes")
            return f"{int(size):,} bytes hashed" if isinstance(size, int | float) else None
        case AuditEvent.ROUTED:
            kind = payload.get("doc_kind") or "document"
            lane = payload.get("lane") or ""
            return f"{kind} · {lane} lane"
        case AuditEvent.EXTRACTION_SUCCEEDED:
            count = payload.get("line_items")
            repairs = payload.get("repair_attempts") or 0
            base = f"{count} line item(s)" if isinstance(count, int) else "fields extracted"
            return f"{base} · {repairs} repair(s)" if repairs else base
        case AuditEvent.VALIDATION_PASSED:
            checks = payload.get("checks")
            return f"{checks} checks passed" if isinstance(checks, int) else "all checks passed"
        case AuditEvent.VALIDATION_FAILED:
            failures = payload.get("failures")
            if isinstance(failures, list) and failures:
                return f"{len(failures)} rule(s) failed"
            return "validation failed"
        case AuditEvent.SCREENING_COMPLETED:
            findings = payload.get("findings")
            history = payload.get("history_size")
            if isinstance(findings, int) and findings:
                return f"{findings} anomaly flag(s)"
            if isinstance(history, int):
                return f"clean against {history} prior invoice(s)"
            return "screened"
        case AuditEvent.LEDGER_COMMITTED:
            return str(payload.get("status") or "committed")
        case _:
            return None


def build_stage_progress(
    events: Sequence[AuditLog], status: DocumentStatus
) -> tuple[list[StageProgress], float]:
    """Project the audit trail onto the six pipeline nodes."""
    states: dict[PipelineStage, StageState] = dict.fromkeys(PIPELINE_ORDER, StageState.PENDING)
    timestamps: dict[PipelineStage, datetime] = {}
    details: dict[PipelineStage, str] = {}
    anomalies_raised = False
    failed_stage: PipelineStage | None = None

    for row in events:
        try:
            event = AuditEvent(row.event)
        except ValueError:  # forward compatibility with newer event names
            continue

        if event is AuditEvent.ANOMALY_RAISED:
            anomalies_raised = True
            continue

        if event is AuditEvent.JOB_FAILED:
            try:
                failed_stage = PipelineStage(row.stage) if row.stage else PipelineStage.INGEST
            except ValueError:
                failed_stage = PipelineStage.INGEST
            timestamps[failed_stage] = row.created_at
            details[failed_stage] = str(row.payload.get("reason") or "failed")
            continue

        completion = _STAGE_COMPLETION.get(event)
        if completion is None:
            continue
        stage, state = completion
        states[stage] = state
        timestamps[stage] = row.created_at
        if (detail := _stage_detail(event, row.payload)) is not None:
            details[stage] = detail

    # A screening pass that produced findings is a flag, not a clean pass.
    if anomalies_raised and states[PipelineStage.SCREEN] is StageState.PASSED:
        states[PipelineStage.SCREEN] = StageState.FLAGGED

    if failed_stage is not None:
        states[failed_stage] = StageState.FAILED
        reached = PIPELINE_ORDER.index(failed_stage)
        for stage in PIPELINE_ORDER[reached + 1 :]:
            states[stage] = StageState.PENDING

    completed = sum(
        1 for state in states.values() if state in {StageState.PASSED, StageState.FLAGGED}
    )

    # While the document is in flight, the first unfinished node is the live one.
    if status is DocumentStatus.PROCESSING:
        for stage in PIPELINE_ORDER:
            if states[stage] is StageState.PENDING:
                states[stage] = StageState.ACTIVE
                break

    progress = completed / len(PIPELINE_ORDER)
    return (
        [
            StageProgress(
                stage=stage,
                state=states[stage],
                detail=details.get(stage),
                at=timestamps.get(stage),
            )
            for stage in PIPELINE_ORDER
        ],
        round(progress, 4),
    )


def to_extraction_out(extraction: Extraction | None) -> ExtractionOut | None:
    if extraction is None:
        return None
    raw_checks = extraction.validation.get("checks", []) if extraction.validation else []
    checks = [ValidationCheck.model_validate(check) for check in raw_checks]
    return ExtractionOut(
        vendor=extraction.vendor,
        invoice_number=extraction.invoice_number,
        issue_date=extraction.issue_date,
        due_date=extraction.due_date,
        line_items=list(extraction.line_items or []),
        subtotal=_as_float(extraction.subtotal),
        tax=_as_float(extraction.tax),
        total=_as_float(extraction.total),
        currency=extraction.currency,
        payment_terms=extraction.payment_terms,
        is_valid=extraction.is_valid,
        repair_attempts=extraction.repair_attempts,
        model=extraction.model,
        lane=Lane(extraction.lane) if extraction.lane else None,
        checks=checks,
    )


def to_anomaly_out(
    anomaly: Anomaly,
    *,
    vendor: str | None = None,
    total: Decimal | None = None,
    currency: str | None = None,
    filename: str | None = None,
) -> AnomalyOut:
    return AnomalyOut(
        id=anomaly.id,
        document_id=anomaly.document_id,
        anomaly_type=AnomalyType(anomaly.anomaly_type),
        severity=AnomalySeverity(anomaly.severity),
        reason=anomaly.reason,
        score=_as_float(anomaly.score),
        evidence=dict(anomaly.evidence or {}),
        status=AnomalyStatus(anomaly.status),
        created_at=anomaly.created_at,
        resolved_at=anomaly.resolved_at,
        resolved_note=anomaly.resolved_note,
        vendor=vendor,
        total=_as_float(total),
        currency=currency,
        filename=filename,
    )


def to_audit_entry(row: AuditLog) -> AuditEntryOut:
    stage: PipelineStage | None
    try:
        stage = PipelineStage(row.stage) if row.stage else None
    except ValueError:
        stage = None
    return AuditEntryOut(
        id=row.id,
        event=row.event,
        stage=stage,
        actor=row.actor,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


def to_trace_out(row: LlmTrace) -> TraceOut:
    return TraceOut(
        id=row.id,
        stage=PipelineStage(row.stage),
        purpose=row.purpose,
        model=row.model,
        mode=row.mode,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
        attempts=row.attempts,
        cost_usd=float(row.cost_usd),
        created_at=row.created_at,
    )


def to_document_summary(
    document: Document,
    *,
    vendor: str | None,
    total: Decimal | None,
    currency: str | None,
    issue_date: date | None,
    anomaly_count: int,
    highest_severity: str | None,
) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        filename=document.filename,
        status=DocumentStatus(document.status),
        doc_kind=DocumentKind(document.doc_kind) if document.doc_kind else None,
        lane=Lane(document.lane) if document.lane else None,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        vendor=vendor,
        total=_as_float(total),
        currency=currency,
        issue_date=issue_date,
        anomaly_count=anomaly_count,
        highest_severity=(AnomalySeverity(highest_severity) if highest_severity else None),
        latency_ms=document.latency_ms,
        cost_usd=float(document.cost_usd),
        created_at=document.created_at,
    )
