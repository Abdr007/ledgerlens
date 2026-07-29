"""`/v1/anomalies` — the review queue and its write-back.

Approve / Reject in the UI lands here and writes to Postgres (spec §6), with the
decision appended to the append-only audit trail so the reason a flag was cleared
is as auditable as the reason it was raised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db, transaction
from app.core.errors import InvalidStateTransitionError, NotFoundError
from app.core.logging import get_logger
from app.models.api import AnomalyOut, ResolveAnomalyRequest
from app.models.enums import AnomalySeverity, AnomalyStatus, AuditEvent, PipelineStage
from app.models.tables import Anomaly, Document, Extraction
from app.pipeline.orchestrator import append_audit
from app.routers.projections import to_anomaly_out
from app.routers.rate_limit import limiter, mutation_rate_limit

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/anomalies", tags=["anomalies"])


@router.get(
    "",
    response_model=list[AnomalyOut],
    summary="List anomalies",
    description="The review queue. Most severe first, then newest.",
)
async def list_anomalies(
    session: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[AnomalyStatus | None, Query(description="Filter by review state.")] = None,
    severity: Annotated[AnomalySeverity | None, Query(description="Filter by severity.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AnomalyOut]:
    filters = []
    if status is not None:
        filters.append(Anomaly.status == str(status))
    if severity is not None:
        filters.append(Anomaly.severity == str(severity))

    rows = (
        await session.execute(
            select(Anomaly, Extraction, Document)
            .join(Document, Document.id == Anomaly.document_id)
            .outerjoin(Extraction, Extraction.document_id == Anomaly.document_id)
            .where(*filters)
            .order_by(Anomaly.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        to_anomaly_out(
            anomaly,
            vendor=extraction.vendor if extraction else None,
            total=extraction.total if extraction else None,
            currency=extraction.currency if extraction else None,
            filename=document.filename,
        )
        for anomaly, extraction, document in rows
    ]
    # Highest severity first so the queue leads with what matters.
    items.sort(key=lambda item: (-item.severity.rank, -item.created_at.timestamp()))
    return items


@router.post(
    "/{anomaly_id}/resolve",
    response_model=AnomalyOut,
    summary="Approve or reject an anomaly",
    description=(
        "Human-in-the-loop decision from the review queue. Approving accepts the "
        "flagged document as legitimate; rejecting confirms the finding. Either way "
        "the decision is appended to the document's audit trail."
    ),
)
@limiter.limit(mutation_rate_limit)
async def resolve_anomaly(
    # Required by name: @limiter.limit reads the client identity from `request`
    # and writes the X-RateLimit-* headers onto `response`.
    request: Request,  # noqa: ARG001
    response: Response,  # noqa: ARG001
    anomaly_id: uuid.UUID,
    payload: ResolveAnomalyRequest,
) -> AnomalyOut:
    target = AnomalyStatus.APPROVED if payload.action == "approve" else AnomalyStatus.REJECTED

    async with transaction() as session:
        anomaly = (
            await session.execute(select(Anomaly).where(Anomaly.id == anomaly_id))
        ).scalar_one_or_none()
        if anomaly is None:
            raise NotFoundError("Anomaly not found.", details={"anomaly_id": str(anomaly_id)})

        current = AnomalyStatus(anomaly.status)
        if current is not AnomalyStatus.OPEN:
            raise InvalidStateTransitionError(
                "This anomaly has already been reviewed.",
                details={"current_status": str(current), "requested": str(target)},
            )

        resolved_at = datetime.now(UTC)
        await session.execute(
            update(Anomaly)
            .where(Anomaly.id == anomaly_id, Anomaly.status == str(AnomalyStatus.OPEN))
            .values(status=str(target), resolved_at=resolved_at, resolved_note=payload.note)
        )
        await append_audit(
            session,
            document_id=anomaly.document_id,
            event=AuditEvent.ANOMALY_RESOLVED,
            stage=PipelineStage.SCREEN,
            actor="reviewer",
            payload={
                "anomaly_id": str(anomaly_id),
                "anomaly_type": anomaly.anomaly_type,
                "severity": anomaly.severity,
                "action": payload.action,
                "resolution": str(target),
                "note": payload.note,
            },
        )

        refreshed = (
            await session.execute(select(Anomaly).where(Anomaly.id == anomaly_id))
        ).scalar_one()
        extraction = (
            await session.execute(
                select(Extraction).where(Extraction.document_id == anomaly.document_id)
            )
        ).scalar_one_or_none()
        document = (
            await session.execute(select(Document).where(Document.id == anomaly.document_id))
        ).scalar_one()

        logger.info(
            "anomaly_resolved",
            extra={
                "anomaly_id": str(anomaly_id),
                "document_id": str(anomaly.document_id),
                "action": payload.action,
            },
        )
        return to_anomaly_out(
            refreshed,
            vendor=extraction.vendor if extraction else None,
            total=extraction.total if extraction else None,
            currency=extraction.currency if extraction else None,
            filename=document.filename,
        )
