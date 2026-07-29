"""Pipeline orchestration — the six stages, the state machine, the ledger.

Ingest -> Route -> Extract -> Validate -> Screen -> Ledger.

Two invariants hold the whole thing together:

**Idempotency (spec §7).** The SHA-256 of the bytes is a `UNIQUE` column, and
ingestion is a single `INSERT ... ON CONFLICT DO NOTHING`. Two browser tabs racing
the same file therefore produce exactly one row — the database decides the winner,
not a read-then-write in Python that would interleave.

**Single ownership.** A document is claimed with a conditional
`UPDATE ... WHERE status = 'PENDING'`. Exactly one caller sees a row come back, so
concurrent processing of the same document is impossible even across processes.

Failure taxonomy is deliberate and total:

* Content the pipeline read but must not auto-commit -> `NEEDS_REVIEW`.
* Infrastructure that would not answer after its retry budget -> `failed_jobs`
  row plus `FAILED`. That is the row spec §9's mid-upload network kill lands in.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.claude import ClaudeClient, LlmUsage
from app.core.db import transaction
from app.core.errors import LedgerLensError
from app.core.files import MediaType, sanitise_filename, sha256_hex, validate_upload
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tracing import TraceContext, Tracer
from app.models.enums import (
    ALLOWED_TRANSITIONS,
    AnomalyStatus,
    AuditEvent,
    DocumentStatus,
    Lane,
    PipelineStage,
)
from app.models.schemas import AnomalyFinding
from app.models.tables import Anomaly, AuditLog, Document, Extraction, FailedJob, LlmTrace
from app.pipeline import extract as extract_stage
from app.pipeline import route as route_stage
from app.pipeline.anomaly import InvoiceRecord, ScreeningConfig, screen_invoice

logger = get_logger(__name__)

# A document still PROCESSING after this long lost its worker to a restart.
STALE_PROCESSING_MINUTES = 15


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """Result of stage 1."""

    document_id: uuid.UUID
    file_hash: str
    filename: str
    status: DocumentStatus
    duplicate: bool
    media_type: MediaType


async def append_audit(
    session: AsyncSession,
    *,
    document_id: uuid.UUID | None,
    event: AuditEvent,
    stage: PipelineStage | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> None:
    """Append one immutable row to the audit log.

    The table has a trigger that rejects UPDATE and DELETE, so this is the only
    way its contents can ever change.
    """
    session.add(
        AuditLog(
            document_id=document_id,
            event=str(event),
            stage=str(stage) if stage else None,
            actor=actor,
            payload=payload or {},
        )
    )


def _record_traces(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    stage: PipelineStage,
    usages: tuple[LlmUsage, ...],
) -> None:
    for usage in usages:
        session.add(
            LlmTrace(
                document_id=document_id,
                stage=str(stage),
                purpose=usage.purpose,
                model=usage.model,
                mode=usage.mode,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=usage.latency_ms,
                attempts=usage.attempts,
                cost_usd=Decimal(str(round(usage.cost_usd, 6))),
            )
        )


class PipelineOrchestrator:
    """Drives a document through all six stages."""

    def __init__(self, *, client: ClaudeClient, settings: Settings, tracer: Tracer) -> None:
        self._client = client
        self._settings = settings
        self._tracer = tracer

    # -- Stage 1: Ingest ---------------------------------------------------

    async def ingest(
        self, *, data: bytes, filename: str, declared_content_type: str | None
    ) -> IngestOutcome:
        """Whitelist, hash and register the upload. Idempotent by content hash."""
        media_type = validate_upload(
            data,
            declared_content_type=declared_content_type,
            max_bytes=self._settings.max_upload_bytes,
        )
        file_hash = sha256_hex(data)
        safe_name = sanitise_filename(filename)

        async with transaction() as session:
            # ON CONFLICT DO NOTHING: the unique index on file_hash is what makes
            # two simultaneous uploads collapse to one row.
            statement = (
                pg_insert(Document)
                .values(
                    file_hash=file_hash,
                    filename=safe_name,
                    media_type=str(media_type),
                    size_bytes=len(data),
                    status=str(DocumentStatus.PENDING),
                )
                .on_conflict_do_nothing(index_elements=[Document.file_hash])
                .returning(Document.id)
            )
            inserted_id = (await session.execute(statement)).scalar_one_or_none()

            if inserted_id is not None:
                await append_audit(
                    session,
                    document_id=inserted_id,
                    event=AuditEvent.DOCUMENT_RECEIVED,
                    stage=PipelineStage.INGEST,
                    payload={
                        "filename": safe_name,
                        "media_type": str(media_type),
                        "size_bytes": len(data),
                        "file_hash": file_hash,
                    },
                )
                return IngestOutcome(
                    document_id=inserted_id,
                    file_hash=file_hash,
                    filename=safe_name,
                    status=DocumentStatus.PENDING,
                    duplicate=False,
                    media_type=media_type,
                )

            existing = (
                await session.execute(select(Document).where(Document.file_hash == file_hash))
            ).scalar_one()
            await append_audit(
                session,
                document_id=existing.id,
                event=AuditEvent.DOCUMENT_DEDUPLICATED,
                stage=PipelineStage.INGEST,
                payload={"filename": safe_name, "existing_status": existing.status},
            )
            return IngestOutcome(
                document_id=existing.id,
                file_hash=file_hash,
                filename=existing.filename,
                status=DocumentStatus(existing.status),
                duplicate=True,
                media_type=media_type,
            )

    # -- State machine -----------------------------------------------------

    async def _claim(self, document_id: uuid.UUID) -> bool:
        """Atomically move PENDING -> PROCESSING. Only one caller can win."""
        async with transaction() as session:
            claimed = (
                await session.execute(
                    update(Document)
                    .where(
                        Document.id == document_id,
                        Document.status == str(DocumentStatus.PENDING),
                    )
                    .values(status=str(DocumentStatus.PROCESSING), updated_at=datetime.now(UTC))
                    .returning(Document.id)
                )
            ).scalar_one_or_none()

            if claimed is None:
                return False

            await append_audit(
                session,
                document_id=document_id,
                event=AuditEvent.STATUS_CHANGED,
                stage=PipelineStage.INGEST,
                payload={
                    "from": str(DocumentStatus.PENDING),
                    "to": str(DocumentStatus.PROCESSING),
                },
            )
            return True

    async def _finalise(
        self,
        *,
        document_id: uuid.UUID,
        target: DocumentStatus,
        reason: str | None,
        latency_ms: int,
        cost_usd: Decimal,
    ) -> None:
        """Move PROCESSING -> terminal, rejecting any transition the machine forbids."""
        async with transaction() as session:
            current_raw = (
                await session.execute(select(Document.status).where(Document.id == document_id))
            ).scalar_one()
            current = DocumentStatus(current_raw)
            if target not in ALLOWED_TRANSITIONS[current]:
                logger.warning(
                    "illegal_status_transition_ignored",
                    extra={
                        "document_id": str(document_id),
                        "from": str(current),
                        "to": str(target),
                    },
                )
                return

            await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.status == str(current))
                .values(
                    status=str(target),
                    status_reason=reason,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                    llm_mode=self._client.mode,
                    updated_at=datetime.now(UTC),
                )
            )
            await append_audit(
                session,
                document_id=document_id,
                event=AuditEvent.STATUS_CHANGED,
                stage=PipelineStage.LEDGER,
                payload={
                    "from": str(current),
                    "to": str(target),
                    "reason": reason,
                    "latency_ms": latency_ms,
                    "cost_usd": float(cost_usd),
                },
            )
            if target in {DocumentStatus.DONE, DocumentStatus.NEEDS_REVIEW}:
                await append_audit(
                    session,
                    document_id=document_id,
                    event=AuditEvent.LEDGER_COMMITTED,
                    stage=PipelineStage.LEDGER,
                    payload={"status": str(target)},
                )

    async def _record_failure(
        self,
        *,
        document_id: uuid.UUID | None,
        file_hash: str | None,
        stage: PipelineStage,
        error_code: str,
        reason: str,
        attempts: int,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Write the `failed_jobs` row and drive the document to FAILED."""
        async with transaction() as session:
            session.add(
                FailedJob(
                    document_id=document_id,
                    file_hash=file_hash,
                    stage=str(stage),
                    error_code=error_code,
                    reason=reason[:4000],
                    attempts=attempts,
                    context=context or {},
                )
            )
            if document_id is not None:
                await session.execute(
                    update(Document)
                    .where(
                        Document.id == document_id,
                        Document.status.in_(
                            [str(DocumentStatus.PENDING), str(DocumentStatus.PROCESSING)]
                        ),
                    )
                    .values(
                        status=str(DocumentStatus.FAILED),
                        status_reason=reason[:1000],
                        updated_at=datetime.now(UTC),
                    )
                )
                await append_audit(
                    session,
                    document_id=document_id,
                    event=AuditEvent.JOB_FAILED,
                    stage=stage,
                    payload={
                        "error_code": error_code,
                        "reason": reason[:1000],
                        "attempts": attempts,
                    },
                )

    # -- Stages 2-6 --------------------------------------------------------

    async def process(
        self, *, document_id: uuid.UUID, data: bytes, filename: str, media_type: MediaType
    ) -> None:
        """Run the pipeline for a claimed document. Never raises."""
        started = time.perf_counter()
        file_hash = sha256_hex(data)
        stage = PipelineStage.ROUTE
        trace = TraceContext(document_id=str(document_id), file_hash=file_hash, stage="route")

        if not await self._claim(document_id):
            logger.info("document_already_claimed", extra={"document_id": str(document_id)})
            return

        total_cost = Decimal("0")

        try:
            # --- Stage 2: Route -------------------------------------------
            routing = await route_stage.route_document(
                client=self._client,
                settings=self._settings,
                data=data,
                media_type=media_type,
                filename=filename,
            )
            total_cost += self._emit_traces(trace, routing.usages)

            async with transaction() as session:
                await session.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(
                        doc_kind=str(routing.decision.doc_kind),
                        lane=str(routing.decision.lane),
                        page_count=routing.page_count,
                        updated_at=datetime.now(UTC),
                    )
                )
                _record_traces(
                    session,
                    document_id=document_id,
                    stage=PipelineStage.ROUTE,
                    usages=routing.usages,
                )
                await append_audit(
                    session,
                    document_id=document_id,
                    event=AuditEvent.ROUTED,
                    stage=PipelineStage.ROUTE,
                    payload={
                        "doc_kind": str(routing.decision.doc_kind),
                        "lane": str(routing.decision.lane),
                        "confidence": routing.decision.confidence,
                        "reason": routing.decision.reason,
                        "model_lane_opinion": str(routing.model_lane_opinion or ""),
                        "lane_overridden_by_measurement": routing.lane_overridden,
                        "pages": routing.page_count,
                    },
                )
                if routing.decision.lane is Lane.TEXT and routing.analysis is not None:
                    await append_audit(
                        session,
                        document_id=document_id,
                        event=AuditEvent.TEXT_EXTRACTED,
                        stage=PipelineStage.ROUTE,
                        payload={
                            "characters": len(routing.analysis.text),
                            "chars_per_page": round(routing.analysis.chars_per_page, 1),
                            "tokens_spent": 0,
                        },
                    )

            # --- Stage 3+4: Extract ---------------------------------------
            stage = PipelineStage.EXTRACT
            trace = TraceContext(document_id=str(document_id), file_hash=file_hash, stage="extract")
            outcome = await extract_stage.extract_document(
                client=self._client,
                settings=self._settings,
                data=data,
                media_type=media_type,
                filename=filename,
                lane=routing.decision.lane,
                analysis=routing.analysis,
            )
            total_cost += self._emit_traces(trace, outcome.usages)

            # --- Stage 5: Validate + persist the extraction ----------------
            stage = PipelineStage.VALIDATE
            async with transaction() as session:
                _record_traces(
                    session,
                    document_id=document_id,
                    stage=PipelineStage.EXTRACT,
                    usages=outcome.usages,
                )
                for attempt in outcome.attempts:
                    await append_audit(
                        session,
                        document_id=document_id,
                        event=(
                            AuditEvent.EXTRACTION_ATTEMPTED
                            if attempt.kind == "initial"
                            else AuditEvent.EXTRACTION_REPAIRED
                        ),
                        stage=PipelineStage.EXTRACT,
                        payload={
                            "attempt": attempt.attempt,
                            "schema_valid": attempt.schema_valid,
                            "rules_passed": attempt.rules_passed,
                            "problem": attempt.problem,
                        },
                    )

                extraction = outcome.extraction
                session.add(
                    Extraction(
                        document_id=document_id,
                        vendor=extraction.vendor,
                        invoice_number=extraction.invoice_number,
                        issue_date=extraction.issue_date,
                        due_date=extraction.due_date,
                        subtotal=extraction.subtotal,
                        tax=extraction.tax,
                        total=extraction.total,
                        currency=extraction.currency,
                        payment_terms=extraction.payment_terms,
                        line_items=[item.model_dump(mode="json") for item in extraction.line_items],
                        raw_payload=outcome.raw_payload,
                        validation=outcome.report.model_dump(mode="json"),
                        is_valid=outcome.report.passed,
                        repair_attempts=outcome.repair_attempts,
                        model=outcome.model,
                        lane=str(outcome.lane),
                    )
                )
                await append_audit(
                    session,
                    document_id=document_id,
                    event=AuditEvent.EXTRACTION_SUCCEEDED,
                    stage=PipelineStage.EXTRACT,
                    payload={
                        "vendor": extraction.vendor,
                        "invoice_number": extraction.invoice_number,
                        "total": float(extraction.total) if extraction.total else None,
                        "currency": extraction.currency,
                        "line_items": len(extraction.line_items),
                        "repair_attempts": outcome.repair_attempts,
                        "lane": str(outcome.lane),
                    },
                )
                await append_audit(
                    session,
                    document_id=document_id,
                    event=(
                        AuditEvent.VALIDATION_PASSED
                        if outcome.report.passed
                        else AuditEvent.VALIDATION_FAILED
                    ),
                    stage=PipelineStage.VALIDATE,
                    payload={
                        "passed": outcome.report.passed,
                        "checks": len(outcome.report.checks),
                        "failures": [check.rule for check in outcome.report.failures],
                        "summary": outcome.report.failure_summary(),
                    },
                )

            # --- Stage 6: Screen ------------------------------------------
            stage = PipelineStage.SCREEN
            findings = await self._screen(document_id=document_id, filename=filename)

            # --- Ledger ---------------------------------------------------
            stage = PipelineStage.LEDGER
            latency_ms = int((time.perf_counter() - started) * 1000)
            blocking = [f for f in findings if f.severity.rank >= 3]
            if outcome.report.passed and not blocking:
                target, reason = DocumentStatus.DONE, None
            elif not outcome.report.passed:
                target = DocumentStatus.NEEDS_REVIEW
                reason = outcome.failure_note or outcome.report.failure_summary()
            else:
                target = DocumentStatus.NEEDS_REVIEW
                reason = blocking[0].reason

            await self._finalise(
                document_id=document_id,
                target=target,
                reason=reason,
                latency_ms=latency_ms,
                cost_usd=total_cost,
            )
            logger.info(
                "document_processed",
                extra={
                    "document_id": str(document_id),
                    "status": str(target),
                    "latency_ms": latency_ms,
                    "cost_usd": float(total_cost),
                    "anomalies": len(findings),
                    "lane": str(routing.decision.lane),
                    "mode": self._client.mode,
                },
            )

        except LedgerLensError as exc:
            await self._record_failure(
                document_id=document_id,
                file_hash=file_hash,
                stage=stage,
                error_code=str(exc.code),
                reason=exc.message,
                attempts=int(exc.details.get("attempts", 1)),
                context=exc.details,
            )
            logger.warning(
                "document_failed",
                extra={
                    "document_id": str(document_id),
                    "stage": str(stage),
                    "error_code": str(exc.code),
                },
            )
        except Exception as exc:  # never let a background task die silently
            await self._record_failure(
                document_id=document_id,
                file_hash=file_hash,
                stage=stage,
                error_code="internal_error",
                reason=f"{type(exc).__name__}: {exc}",
                attempts=1,
            )
            logger.exception(
                "document_failed_unexpectedly",
                extra={"document_id": str(document_id), "stage": str(stage)},
            )

    def _emit_traces(self, context: TraceContext, usages: tuple[LlmUsage, ...]) -> Decimal:
        total = Decimal("0")
        for usage in usages:
            self._tracer.on_llm_call(
                context,
                usage,
                input_preview=f"{usage.purpose} call",
                output_preview=f"{usage.output_tokens} output tokens",
            )
            total += Decimal(str(round(usage.cost_usd, 6)))
        return total

    async def _screen(self, *, document_id: uuid.UUID, filename: str) -> list[AnomalyFinding]:
        """Stage 6 — compare against vendor history and persist findings."""
        config = ScreeningConfig(
            vendor_similarity=self._settings.duplicate_vendor_similarity,
            amount_tolerance_pct=self._settings.duplicate_amount_tolerance_pct,
            date_window_days=self._settings.duplicate_date_window_days,
            zscore_threshold=self._settings.zscore_threshold,
            min_history_for_zscore=self._settings.min_history_for_zscore,
        )

        async with transaction() as session:
            rows = (
                await session.execute(
                    select(
                        Extraction.document_id,
                        Extraction.vendor,
                        Extraction.invoice_number,
                        Extraction.issue_date,
                        Extraction.total,
                        Extraction.currency,
                        Extraction.payment_terms,
                        Document.filename,
                    ).join(Document, Document.id == Extraction.document_id)
                )
            ).all()

            records = [
                InvoiceRecord(
                    document_id=row.document_id,
                    vendor=row.vendor,
                    invoice_number=row.invoice_number,
                    issue_date=row.issue_date,
                    total=row.total,
                    currency=row.currency,
                    payment_terms=row.payment_terms,
                    filename=row.filename,
                )
                for row in rows
            ]
            candidate = next((r for r in records if r.document_id == document_id), None)
            if candidate is None:
                return []

            history = [r for r in records if r.document_id != document_id]
            findings = screen_invoice(candidate, history, config)

            for finding in findings:
                await session.execute(
                    pg_insert(Anomaly)
                    .values(
                        document_id=document_id,
                        anomaly_type=str(finding.anomaly_type),
                        severity=str(finding.severity),
                        reason=finding.reason,
                        score=(
                            Decimal(str(round(finding.score, 4)))
                            if finding.score is not None
                            else None
                        ),
                        evidence=finding.evidence,
                        fingerprint=finding.fingerprint,
                        status=str(AnomalyStatus.OPEN),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[Anomaly.document_id, Anomaly.fingerprint]
                    )
                )
                await append_audit(
                    session,
                    document_id=document_id,
                    event=AuditEvent.ANOMALY_RAISED,
                    stage=PipelineStage.SCREEN,
                    payload={
                        "anomaly_type": str(finding.anomaly_type),
                        "severity": str(finding.severity),
                        "reason": finding.reason,
                        "score": finding.score,
                    },
                )

            await append_audit(
                session,
                document_id=document_id,
                event=AuditEvent.SCREENING_COMPLETED,
                stage=PipelineStage.SCREEN,
                payload={
                    "history_size": len(history),
                    "findings": len(findings),
                    "highest_severity": (
                        str(max(findings, key=lambda f: f.severity.rank).severity)
                        if findings
                        else None
                    ),
                    "filename": filename,
                },
            )
            return findings


async def recover_stale_documents() -> int:
    """Fail documents whose worker vanished (process restart, container eviction).

    Without this a crash leaves rows stuck in PROCESSING for ever and the UI spins
    on a document nobody is working on.
    """
    cutoff = datetime.now(UTC).timestamp() - STALE_PROCESSING_MINUTES * 60
    recovered = 0
    async with transaction() as session:
        stale = (
            await session.execute(
                select(Document.id, Document.file_hash, Document.status).where(
                    Document.status.in_(
                        [str(DocumentStatus.PENDING), str(DocumentStatus.PROCESSING)]
                    ),
                    func.extract("epoch", Document.updated_at) < cutoff,
                )
            )
        ).all()

        for row in stale:
            session.add(
                FailedJob(
                    document_id=row.id,
                    file_hash=row.file_hash,
                    stage=str(PipelineStage.INGEST),
                    error_code="worker_lost",
                    reason=(
                        "Processing was interrupted before it completed — the worker "
                        "was restarted or evicted. Re-upload the document to retry."
                    ),
                    attempts=1,
                    context={"previous_status": row.status},
                )
            )
            await session.execute(
                update(Document)
                .where(Document.id == row.id, Document.status == row.status)
                .values(
                    status=str(DocumentStatus.FAILED),
                    status_reason="Interrupted before completion (worker restarted).",
                    updated_at=datetime.now(UTC),
                )
            )
            await append_audit(
                session,
                document_id=row.id,
                event=AuditEvent.JOB_FAILED,
                stage=PipelineStage.INGEST,
                payload={"error_code": "worker_lost", "previous_status": row.status},
            )
            recovered += 1

    if recovered:
        logger.warning("stale_documents_recovered", extra={"count": recovered})
    return recovered
