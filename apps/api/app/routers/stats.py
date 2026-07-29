"""`/v1/stats` — the KPI cards and the vendor-spend chart.

Everything is aggregated in Postgres in a handful of queries rather than pulled
into Python, so the dashboard stays cheap as the ledger grows.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Float, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.settings import Settings
from app.deps import get_app_settings, get_claude_client
from app.models.api import StatsResponse, VendorSpend
from app.models.enums import AnomalyStatus, DocumentStatus
from app.models.tables import Anomaly, Document, Extraction

router = APIRouter(prefix="/v1/stats", tags=["stats"])

_VENDOR_LIMIT = 8


@router.get(
    "",
    response_model=StatsResponse,
    summary="Dashboard statistics",
    description="Counts, latency percentiles, estimated spend and vendor totals.",
)
async def get_stats(
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> StatsResponse:
    status_rows = (
        await session.execute(select(Document.status, func.count()).group_by(Document.status))
    ).all()
    status_breakdown = {status: int(count) for status, count in status_rows}
    documents_total = sum(status_breakdown.values())

    # Latency and cost are only meaningful for documents that finished the pipeline.
    terminal = [
        str(DocumentStatus.DONE),
        str(DocumentStatus.NEEDS_REVIEW),
    ]
    latency_row = (
        await session.execute(
            select(
                func.coalesce(func.avg(Document.latency_ms), 0.0),
                func.coalesce(
                    func.percentile_cont(0.95).within_group(Document.latency_ms.cast(Float).asc()),
                    0.0,
                ),
                func.coalesce(func.sum(Document.cost_usd), 0),
                func.count(),
            ).where(Document.status.in_(terminal), Document.latency_ms.is_not(None))
        )
    ).one()
    avg_latency, p95_latency, total_cost, processed = latency_row

    anomaly_rows = (
        await session.execute(select(Anomaly.status, func.count()).group_by(Anomaly.status))
    ).all()
    anomalies_by_status = {status: int(count) for status, count in anomaly_rows}
    anomalies_total = sum(anomalies_by_status.values())
    anomalies_open = anomalies_by_status.get(str(AnomalyStatus.OPEN), 0)

    severity_rows = (
        await session.execute(
            select(Anomaly.severity, func.count())
            .where(Anomaly.status == str(AnomalyStatus.OPEN))
            .group_by(Anomaly.severity)
        )
    ).all()
    type_rows = (
        await session.execute(
            select(Anomaly.anomaly_type, func.count())
            .where(Anomaly.status == str(AnomalyStatus.OPEN))
            .group_by(Anomaly.anomaly_type)
        )
    ).all()

    vendor_rows = (
        await session.execute(
            select(
                Extraction.vendor,
                func.sum(Extraction.total),
                func.count(),
                # A vendor bills in one currency in this corpus; surface the
                # dominant one rather than silently summing across currencies.
                func.max(Extraction.currency),
            )
            .join(Document, Document.id == Extraction.document_id)
            .where(
                Extraction.vendor.is_not(None),
                Extraction.total.is_not(None),
                Document.status.in_(terminal),
            )
            .group_by(Extraction.vendor)
            .order_by(func.sum(Extraction.total).desc())
            .limit(_VENDOR_LIMIT)
        )
    ).all()

    processed_count = int(processed)
    total_cost_float = float(total_cost or 0)

    return StatsResponse(
        documents_total=documents_total,
        documents_processed=status_breakdown.get(str(DocumentStatus.DONE), 0),
        documents_needs_review=status_breakdown.get(str(DocumentStatus.NEEDS_REVIEW), 0),
        documents_failed=status_breakdown.get(str(DocumentStatus.FAILED), 0),
        anomalies_open=anomalies_open,
        anomalies_total=anomalies_total,
        avg_latency_ms=round(float(avg_latency or 0), 1),
        p95_latency_ms=round(float(p95_latency or 0), 1),
        est_cost_usd=round(total_cost_float, 6),
        avg_cost_per_document_usd=(
            round(total_cost_float / processed_count, 6) if processed_count else 0.0
        ),
        llm_mode=get_claude_client().mode,
        router_model=settings.model_router,
        extractor_model=settings.model_extractor,
        vendor_spend=[
            VendorSpend(
                vendor=vendor,
                total=round(float(total or 0), 2),
                invoice_count=int(count),
                currency=currency,
            )
            for vendor, total, count, currency in vendor_rows
        ],
        status_breakdown=status_breakdown,
        severity_breakdown={severity: int(count) for severity, count in severity_rows},
        anomaly_type_breakdown={kind: int(count) for kind, count in type_rows},
    )
