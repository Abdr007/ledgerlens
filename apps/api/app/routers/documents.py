"""`/v1/documents` — ingestion and the polling surface the pipeline visual reads."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import FileTooLargeError, NotFoundError
from app.core.logging import get_logger
from app.core.settings import Settings
from app.deps import get_app_settings, get_orchestrator
from app.models.api import (
    AuditEntryOut,
    DocumentDetail,
    DocumentListResponse,
    DocumentStatusResponse,
    TraceOut,
    UploadResponse,
)
from app.models.enums import AnomalySeverity, AnomalyStatus, DocumentStatus
from app.models.tables import Anomaly, AuditLog, Document, Extraction, LlmTrace
from app.pipeline.orchestrator import PipelineOrchestrator
from app.routers.projections import (
    build_stage_progress,
    to_anomaly_out,
    to_audit_entry,
    to_document_summary,
    to_extraction_out,
    to_trace_out,
)
from app.routers.rate_limit import limiter, upload_rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/documents", tags=["documents"])

_READ_CHUNK = 64 * 1024


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read at most `max_bytes`, then stop.

    Reading the whole stream first and checking the length afterwards would let a
    hostile client push arbitrary bytes into memory before being rejected.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLargeError(
                f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.",
                details={"max_bytes": max_bytes},
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _severity_map(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, str | None]]:
    """Open-anomaly count and highest severity per document, in one round trip."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Anomaly.document_id, Anomaly.severity).where(
                Anomaly.document_id.in_(ids), Anomaly.status == str(AnomalyStatus.OPEN)
            )
        )
    ).all()
    summary: dict[uuid.UUID, tuple[int, str | None]] = {}
    for document_id, severity in rows:
        count, current = summary.get(document_id, (0, None))
        best = severity
        if current is not None and AnomalySeverity(current).rank >= AnomalySeverity(severity).rank:
            best = current
        summary[document_id] = (count + 1, best)
    return summary


@router.post(
    "",
    response_model=UploadResponse,
    status_code=202,
    summary="Upload a document",
    description=(
        "Accepts a PDF, PNG or JPEG (max 10 MB). The SHA-256 of the file is the "
        "idempotency key: re-uploading identical bytes returns the existing record "
        "without reprocessing. Processing continues in the background — poll "
        "`/v1/documents/{id}/status` to drive the pipeline visual."
    ),
)
@limiter.limit(upload_rate_limit)
async def upload_document(
    # `request` and `response` are unused in the body but required by name:
    # @limiter.limit reads the client identity from `request` and writes the
    # X-RateLimit-* headers onto `response`.
    request: Request,  # noqa: ARG001
    response: Response,  # noqa: ARG001
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="PDF, PNG or JPEG, 10 MB maximum.")],
    settings: Annotated[Settings, Depends(get_app_settings)],
    orchestrator: Annotated[PipelineOrchestrator, Depends(get_orchestrator)],
) -> UploadResponse:
    data = await _read_capped(file, settings.max_upload_bytes)
    outcome = await orchestrator.ingest(
        data=data,
        filename=file.filename or "document",
        declared_content_type=file.content_type,
    )

    if not outcome.duplicate:
        background.add_task(
            orchestrator.process,
            document_id=outcome.document_id,
            data=data,
            filename=outcome.filename,
            media_type=outcome.media_type,
        )
    else:
        logger.info(
            "upload_deduplicated",
            extra={"document_id": str(outcome.document_id), "file_hash": outcome.file_hash},
        )

    return UploadResponse(
        document_id=outcome.document_id,
        file_hash=outcome.file_hash,
        filename=outcome.filename,
        status=outcome.status,
        duplicate=outcome.duplicate,
    )


@router.get("", response_model=DocumentListResponse, summary="List documents")
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[DocumentStatus | None, Query(description="Filter by status.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    filters = [Document.status == str(status)] if status else []

    total = (
        await session.execute(select(func.count()).select_from(Document).where(*filters))
    ).scalar_one()

    rows = (
        await session.execute(
            select(Document, Extraction)
            .outerjoin(Extraction, Extraction.document_id == Document.id)
            .where(*filters)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    documents = [row[0] for row in rows]
    severities = await _severity_map(session, [doc.id for doc in documents])

    items = []
    for document, extraction in rows:
        count, highest = severities.get(document.id, (0, None))
        items.append(
            to_document_summary(
                document,
                vendor=extraction.vendor if extraction else None,
                total=extraction.total if extraction else None,
                currency=extraction.currency if extraction else None,
                issue_date=extraction.issue_date if extraction else None,
                anomaly_count=count,
                highest_severity=highest,
            )
        )

    return DocumentListResponse(items=items, total=total, limit=limit, offset=offset)


async def _load_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = (
        await session.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document not found.", details={"document_id": str(document_id)})
    return document


@router.get(
    "/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Poll pipeline status",
    description=(
        "Drives the six-stage pipeline animation. Stage states are projected from "
        "the append-only audit log, so they reflect work that actually happened."
    ),
)
async def get_document_status(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentStatusResponse:
    document = await _load_document(session, document_id)
    status = DocumentStatus(document.status)

    events = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == document_id)
                .order_by(AuditLog.id.asc())
            )
        ).scalars()
    )
    stages, progress = build_stage_progress(events, status)

    extraction = (
        await session.execute(select(Extraction).where(Extraction.document_id == document_id))
    ).scalar_one_or_none()

    open_anomalies = list(
        (
            await session.execute(
                select(Anomaly).where(
                    Anomaly.document_id == document_id,
                    Anomaly.status == str(AnomalyStatus.OPEN),
                )
            )
        ).scalars()
    )
    highest = (
        max((AnomalySeverity(a.severity) for a in open_anomalies), key=lambda s: s.rank)
        if open_anomalies
        else None
    )

    return DocumentStatusResponse(
        document_id=document.id,
        status=status,
        status_reason=document.status_reason,
        doc_kind=document.doc_kind,
        lane=document.lane,
        stages=stages,
        progress=progress,
        is_terminal=status.is_terminal,
        latency_ms=document.latency_ms,
        cost_usd=float(document.cost_usd),
        anomaly_count=len(open_anomalies),
        highest_severity=highest,
        extraction=to_extraction_out(extraction),
        updated_at=document.updated_at,
    )


@router.get("/{document_id}", response_model=DocumentDetail, summary="Document detail")
async def get_document(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentDetail:
    document = await _load_document(session, document_id)

    extraction = (
        await session.execute(select(Extraction).where(Extraction.document_id == document_id))
    ).scalar_one_or_none()
    anomalies = list(
        (
            await session.execute(
                select(Anomaly)
                .where(Anomaly.document_id == document_id)
                .order_by(Anomaly.created_at.desc())
            )
        ).scalars()
    )
    audit = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == document_id)
                .order_by(AuditLog.id.asc())
            )
        ).scalars()
    )
    traces = list(
        (
            await session.execute(
                select(LlmTrace)
                .where(LlmTrace.document_id == document_id)
                .order_by(LlmTrace.created_at.asc())
            )
        ).scalars()
    )

    return DocumentDetail(
        id=document.id,
        file_hash=document.file_hash,
        filename=document.filename,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        status=DocumentStatus(document.status),
        status_reason=document.status_reason,
        doc_kind=document.doc_kind,
        lane=document.lane,
        page_count=document.page_count,
        latency_ms=document.latency_ms,
        cost_usd=float(document.cost_usd),
        llm_mode=document.llm_mode,
        created_at=document.created_at,
        updated_at=document.updated_at,
        extraction=to_extraction_out(extraction),
        anomalies=[
            to_anomaly_out(
                anomaly,
                vendor=extraction.vendor if extraction else None,
                total=extraction.total if extraction else None,
                currency=extraction.currency if extraction else None,
                filename=document.filename,
            )
            for anomaly in anomalies
        ],
        audit=[to_audit_entry(row) for row in audit],
        traces=[to_trace_out(row) for row in traces],
    )


@router.get(
    "/{document_id}/audit",
    response_model=list[AuditEntryOut],
    summary="Append-only audit trail",
    description="Every state change for this document, oldest first. Never updated or deleted.",
)
async def get_document_audit(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[AuditEntryOut]:
    await _load_document(session, document_id)
    rows = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.document_id == document_id)
                .order_by(AuditLog.id.asc())
            )
        ).scalars()
    )
    return [to_audit_entry(row) for row in rows]


@router.get(
    "/{document_id}/traces",
    response_model=list[TraceOut],
    summary="LLM traces for this document",
    description="Tokens, cost, latency and retry count for every model call.",
)
async def get_document_traces(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[TraceOut]:
    await _load_document(session, document_id)
    rows = list(
        (
            await session.execute(
                select(LlmTrace)
                .where(LlmTrace.document_id == document_id)
                .order_by(LlmTrace.created_at.asc())
            )
        ).scalars()
    )
    return [to_trace_out(row) for row in rows]
