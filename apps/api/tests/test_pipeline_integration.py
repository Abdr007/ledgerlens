"""Integration tests against a real PostgreSQL database.

These cover the Definition of Done items that only mean something against real
Postgres (spec §8c and the three post-build checks in spec §9):

* idempotent re-upload — the same bytes twice produce exactly one record
* **two concurrent uploads of the same file** — exactly one row survives
* **two concurrent processors of the same document** — exactly one claims it
* status transitions follow `PENDING -> PROCESSING -> DONE / NEEDS_REVIEW`
* a mid-pipeline failure lands in `failed_jobs` with a reason
* the audit log genuinely rejects UPDATE and DELETE
* the planted duplicate raises a HIGH-severity anomaly with an explanation
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.claude import LlmRequest, LlmResult, LlmUsage
from app.core.db import transaction
from app.core.errors import UpstreamUnavailableError
from app.core.settings import Settings
from app.core.tracing import LocalTracer
from app.devtools.corpus import build_seed_corpus
from app.devtools.documents import InvoiceSpec, render_invoice_pdf
from app.models.enums import AnomalySeverity, AnomalyType, AuditEvent, DocumentStatus, Lane
from app.models.tables import Anomaly, AuditLog, Document, Extraction, FailedJob, LlmTrace
from app.pipeline.orchestrator import PipelineOrchestrator

pytestmark = pytest.mark.integration


async def _ingest_and_process(
    orchestrator: PipelineOrchestrator, payload: bytes, filename: str
) -> uuid.UUID:
    outcome = await orchestrator.ingest(
        data=payload, filename=filename, declared_content_type="application/pdf"
    )
    await orchestrator.process(
        document_id=outcome.document_id,
        data=payload,
        filename=filename,
        media_type=outcome.media_type,
    )
    return outcome.document_id


async def _document(document_id: uuid.UUID) -> Document:
    async with transaction() as session:
        return (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one()


async def _count(model: Any) -> int:
    async with transaction() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_clean_invoice_completes_and_persists_everything(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes, sample_spec: InvoiceSpec
) -> None:
    document_id = await _ingest_and_process(orchestrator, sample_pdf, "invoice.pdf")
    document = await _document(document_id)

    assert DocumentStatus(document.status) is DocumentStatus.DONE
    assert document.lane == str(Lane.TEXT), "a digital PDF must never reach the vision lane"
    assert document.latency_ms is not None and document.latency_ms >= 0
    assert document.page_count == 1

    async with transaction() as session:
        extraction = (
            await session.execute(select(Extraction).where(Extraction.document_id == document_id))
        ).scalar_one()

    assert extraction.is_valid
    assert extraction.vendor == sample_spec.vendor
    assert extraction.invoice_number == sample_spec.invoice_number
    assert extraction.total == sample_spec.total
    assert extraction.issue_date == sample_spec.issue_date
    assert len(extraction.line_items) == len(sample_spec.lines)

    # Every model call is traced (spec §2, observability).
    assert await _count(LlmTrace) >= 2


async def test_audit_trail_records_every_stage(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    document_id = await _ingest_and_process(orchestrator, sample_pdf, "invoice.pdf")
    async with transaction() as session:
        events = [
            row.event
            for row in (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.document_id == document_id)
                    .order_by(AuditLog.id)
                )
            ).scalars()
        ]

    for required in (
        AuditEvent.DOCUMENT_RECEIVED,
        AuditEvent.ROUTED,
        AuditEvent.EXTRACTION_SUCCEEDED,
        AuditEvent.VALIDATION_PASSED,
        AuditEvent.SCREENING_COMPLETED,
        AuditEvent.LEDGER_COMMITTED,
    ):
        assert str(required) in events, f"missing {required} in {events}"


# ---------------------------------------------------------------------------
# Idempotency (spec §7, §9 check 3)
# ---------------------------------------------------------------------------


async def test_reupload_returns_the_existing_record(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    first = await orchestrator.ingest(
        data=sample_pdf, filename="a.pdf", declared_content_type="application/pdf"
    )
    second = await orchestrator.ingest(
        data=sample_pdf, filename="renamed.pdf", declared_content_type="application/pdf"
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.document_id == first.document_id
    assert await _count(Document) == 1


async def test_concurrent_uploads_of_the_same_file_create_one_row(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    """Spec §9, check 3: the same file uploaded twice, fast, from two tabs."""
    results = await asyncio.gather(
        *(
            orchestrator.ingest(
                data=sample_pdf,
                filename=f"tab-{index}.pdf",
                declared_content_type="application/pdf",
            )
            for index in range(8)
        )
    )

    assert await _count(Document) == 1, "UNIQUE(file_hash) must collapse the race to one row"
    assert len({outcome.document_id for outcome in results}) == 1
    assert sum(1 for outcome in results if not outcome.duplicate) == 1, (
        "exactly one caller may be told it created the record"
    )


async def test_different_bytes_are_not_deduplicated(
    clean_db: None, orchestrator: PipelineOrchestrator, invoice_specs: list[InvoiceSpec]
) -> None:
    for spec in invoice_specs[:3]:
        await orchestrator.ingest(
            data=render_invoice_pdf(spec),
            filename=f"{spec.invoice_number}.pdf",
            declared_content_type="application/pdf",
        )
    assert await _count(Document) == 3


# ---------------------------------------------------------------------------
# Status transitions under concurrency (spec §8c)
# ---------------------------------------------------------------------------


async def test_only_one_processor_can_claim_a_document(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    """Two workers racing the same document must not both process it."""
    outcome = await orchestrator.ingest(
        data=sample_pdf, filename="race.pdf", declared_content_type="application/pdf"
    )

    await asyncio.gather(
        *(
            orchestrator.process(
                document_id=outcome.document_id,
                data=sample_pdf,
                filename="race.pdf",
                media_type=outcome.media_type,
            )
            for _ in range(6)
        )
    )

    # The extraction table has a UNIQUE(document_id); a second processor would
    # have raised on insert and landed the document in FAILED.
    assert await _count(Extraction) == 1
    document = await _document(outcome.document_id)
    assert DocumentStatus(document.status) is DocumentStatus.DONE

    async with transaction() as session:
        transitions = [
            row.payload
            for row in (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.document_id == outcome.document_id,
                        AuditLog.event == str(AuditEvent.STATUS_CHANGED),
                    )
                )
            ).scalars()
        ]
    # PENDING->PROCESSING exactly once, then PROCESSING->DONE exactly once.
    assert sum(1 for p in transitions if p["to"] == str(DocumentStatus.PROCESSING)) == 1
    assert sum(1 for p in transitions if p["to"] == str(DocumentStatus.DONE)) == 1


async def test_terminal_documents_are_not_reprocessed(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    outcome = await orchestrator.ingest(
        data=sample_pdf, filename="once.pdf", declared_content_type="application/pdf"
    )
    for _ in range(3):
        await orchestrator.process(
            document_id=outcome.document_id,
            data=sample_pdf,
            filename="once.pdf",
            media_type=outcome.media_type,
        )
    assert await _count(Extraction) == 1


# ---------------------------------------------------------------------------
# Failure handling (spec §7, §9 check 2)
# ---------------------------------------------------------------------------


class _UnreachableClaudeClient:
    """Stands in for the model being unreachable after every retry."""

    mode = "live"

    async def invoke_tool(self, request: LlmRequest) -> LlmResult:
        raise UpstreamUnavailableError(
            "The extraction model is temporarily unavailable.",
            details={"operation": f"claude.{request.purpose}", "attempts": 3},
        )

    async def aclose(self) -> None:
        return None


async def test_upstream_failure_lands_in_failed_jobs_with_a_reason(
    clean_db: None, settings: Settings, sample_pdf: bytes
) -> None:
    """Spec §9, check 2: kill the network mid-upload."""
    broken = PipelineOrchestrator(
        client=_UnreachableClaudeClient(), settings=settings, tracer=LocalTracer()
    )
    document_id = await _ingest_and_process(broken, sample_pdf, "offline.pdf")

    document = await _document(document_id)
    assert DocumentStatus(document.status) is DocumentStatus.FAILED

    async with transaction() as session:
        job = (
            await session.execute(select(FailedJob).where(FailedJob.document_id == document_id))
        ).scalar_one()

    assert job.error_code == "upstream_unavailable"
    assert job.reason  # a human-readable reason, not an empty string
    assert job.attempts == 3
    assert job.stage == "route"


async def test_failure_is_recorded_in_the_audit_trail(
    clean_db: None, settings: Settings, sample_pdf: bytes
) -> None:
    broken = PipelineOrchestrator(
        client=_UnreachableClaudeClient(), settings=settings, tracer=LocalTracer()
    )
    document_id = await _ingest_and_process(broken, sample_pdf, "offline.pdf")

    async with transaction() as session:
        events = [
            row.event
            for row in (
                await session.execute(select(AuditLog).where(AuditLog.document_id == document_id))
            ).scalars()
        ]
    assert str(AuditEvent.JOB_FAILED) in events


class _MalformedClaudeClient:
    """Returns a payload the schema rejects, on every attempt."""

    mode = "live"

    def __init__(self) -> None:
        self.calls = 0

    async def invoke_tool(self, request: LlmRequest) -> LlmResult:
        self.calls += 1
        payload: dict[str, Any] = (
            {"doc_kind": "invoice", "lane": "digital", "confidence": 0.9, "reason": "ok"}
            if request.tool.name == "classify_document"
            else {"vendor": "X", "total": "not-a-number", "hallucinated_field": True}
        )
        return LlmResult(
            payload=payload,
            usage=LlmUsage(
                model="test",
                mode="live",
                purpose=request.purpose,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                attempts=1,
                cost_usd=0.0,
            ),
        )


async def test_unrepairable_extraction_routes_to_review_not_the_ledger(
    clean_db: None, settings: Settings, sample_pdf: bytes
) -> None:
    """Never auto-commit something the schema could not validate."""
    client = _MalformedClaudeClient()
    orchestrator = PipelineOrchestrator(client=client, settings=settings, tracer=LocalTracer())
    document_id = await _ingest_and_process(orchestrator, sample_pdf, "garbage.pdf")

    document = await _document(document_id)
    assert DocumentStatus(document.status) is DocumentStatus.NEEDS_REVIEW
    assert document.status_reason

    # 1 routing call + 1 initial extraction + 2 repairs (spec: max 2 retries).
    assert client.calls == 1 + (1 + settings.extraction_max_repair_attempts)


# ---------------------------------------------------------------------------
# Append-only audit log
# ---------------------------------------------------------------------------


async def test_audit_log_rejects_update(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    await _ingest_and_process(orchestrator, sample_pdf, "audit.pdf")
    with pytest.raises(Exception, match="append-only"):
        async with transaction() as session:
            await session.execute(text("UPDATE audit_log SET actor = 'tamper'"))


async def test_audit_log_rejects_delete(
    clean_db: None, orchestrator: PipelineOrchestrator, sample_pdf: bytes
) -> None:
    await _ingest_and_process(orchestrator, sample_pdf, "audit.pdf")
    with pytest.raises(Exception, match="append-only"):
        async with transaction() as session:
            await session.execute(text("DELETE FROM audit_log"))


# ---------------------------------------------------------------------------
# Duplicate detection end to end (spec §8e)
# ---------------------------------------------------------------------------


async def test_planted_duplicate_raises_a_high_severity_anomaly(
    clean_db: None, orchestrator: PipelineOrchestrator
) -> None:
    """Spec §8, Definition of Done (e)."""
    corpus = build_seed_corpus()
    metals = [item for item in corpus if item.spec.vendor.startswith("Gulf Metals")]
    assert any(item.expected_anomalies for item in metals), "corpus must plant a duplicate"

    document_ids: list[uuid.UUID] = []
    for item in metals:
        document_ids.append(
            await _ingest_and_process(orchestrator, render_invoice_pdf(item.spec), item.filename)
        )

    planted_index = next(index for index, item in enumerate(metals) if item.expected_anomalies)

    async with transaction() as session:
        anomalies = list(
            (
                await session.execute(
                    select(Anomaly).where(Anomaly.document_id == document_ids[planted_index])
                )
            ).scalars()
        )

    assert len(anomalies) == 1
    finding = anomalies[0]
    assert AnomalyType(finding.anomaly_type) is AnomalyType.DUPLICATE
    assert AnomalySeverity(finding.severity) is AnomalySeverity.HIGH
    # A plain-English reason a finance team can act on.
    assert "duplicate" in finding.reason.lower()
    assert "already billed" in finding.reason.lower()
    assert finding.evidence["matched_document_id"]
    assert finding.evidence["day_gap"] <= 7

    # A HIGH finding must block auto-commit.
    document = await _document(document_ids[planted_index])
    assert DocumentStatus(document.status) is DocumentStatus.NEEDS_REVIEW


async def test_clean_history_raises_no_anomalies(
    clean_db: None, orchestrator: PipelineOrchestrator, invoice_specs: list[InvoiceSpec]
) -> None:
    """Precision matters as much as recall: a noisy queue gets ignored."""
    for spec in invoice_specs[:4]:
        await _ingest_and_process(
            orchestrator, render_invoice_pdf(spec), f"{spec.invoice_number}.pdf"
        )
    assert await _count(Anomaly) == 0


async def test_rescreening_does_not_duplicate_findings(
    clean_db: None, orchestrator: PipelineOrchestrator
) -> None:
    """The unique fingerprint stops a re-run from multiplying the queue."""
    corpus = build_seed_corpus()
    metals = [item for item in corpus if item.spec.vendor.startswith("Gulf Metals")]
    ids = [
        await _ingest_and_process(orchestrator, render_invoice_pdf(item.spec), item.filename)
        for item in metals
    ]
    before = await _count(Anomaly)

    planted = next(index for index, item in enumerate(metals) if item.expected_anomalies)
    # Deliberately re-invokes the screening stage directly: the guarantee under
    # test is the unique fingerprint, not the public entry point.
    await orchestrator._screen(document_id=ids[planted], filename="rerun.pdf")

    assert await _count(Anomaly) == before
