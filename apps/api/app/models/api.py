"""HTTP request/response models.

Everything the web client sees is defined here, so the OpenAPI document at
`/docs` is a complete, accurate description of the API surface. Money crosses the
wire as `float` for the chart libraries; it is `Decimal` everywhere it matters
(database columns and the validation arithmetic).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    DocumentKind,
    DocumentStatus,
    Lane,
    PipelineStage,
    StageState,
)
from app.models.schemas import ValidationCheck


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """The only error shape the API ever returns (spec §7: no stack traces)."""

    error: ErrorDetail


class UploadResponse(BaseModel):
    """Result of `POST /v1/documents`."""

    document_id: uuid.UUID
    file_hash: str = Field(description="SHA-256 of the file bytes — the idempotency key.")
    filename: str
    status: DocumentStatus
    duplicate: bool = Field(
        description="True when these exact bytes were already ingested; no new record was created."
    )


class StageProgress(BaseModel):
    """One node in the six-stage pipeline visual."""

    stage: PipelineStage
    state: StageState
    detail: str | None = None
    at: datetime | None = None


class ExtractionOut(BaseModel):
    """Schema-validated fields, as rendered in the result card."""

    vendor: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    payment_terms: str | None = None
    is_valid: bool = False
    repair_attempts: int = 0
    model: str | None = None
    lane: Lane | None = None
    checks: list[ValidationCheck] = Field(default_factory=list)


class AnomalyOut(BaseModel):
    """A review-queue card: severity, plain-English reason, evidence."""

    id: uuid.UUID
    document_id: uuid.UUID
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    reason: str
    score: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: AnomalyStatus
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_note: str | None = None
    vendor: str | None = None
    total: float | None = None
    currency: str | None = None
    filename: str | None = None


class DocumentStatusResponse(BaseModel):
    """`GET /v1/documents/{id}/status` — what drives the animated pipeline.

    The stage list is derived from the append-only audit log, so the UI reflects
    what actually happened rather than a client-side timer.
    """

    document_id: uuid.UUID
    status: DocumentStatus
    status_reason: str | None = None
    doc_kind: DocumentKind | None = None
    lane: Lane | None = None
    stages: list[StageProgress]
    progress: float = Field(ge=0.0, le=1.0)
    is_terminal: bool
    latency_ms: int | None = None
    cost_usd: float = 0.0
    anomaly_count: int = 0
    highest_severity: AnomalySeverity | None = None
    extraction: ExtractionOut | None = None
    updated_at: datetime


class DocumentSummary(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus
    doc_kind: DocumentKind | None = None
    lane: Lane | None = None
    media_type: str
    size_bytes: int
    vendor: str | None = None
    total: float | None = None
    currency: str | None = None
    issue_date: date | None = None
    anomaly_count: int = 0
    highest_severity: AnomalySeverity | None = None
    latency_ms: int | None = None
    cost_usd: float = 0.0
    created_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    limit: int
    offset: int


class AuditEntryOut(BaseModel):
    """One row of the append-only trail rendered in the Audit Trail drawer."""

    id: int
    event: str
    stage: PipelineStage | None = None
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TraceOut(BaseModel):
    """One LLM call: tokens, cost, latency, retries."""

    id: uuid.UUID
    stage: PipelineStage
    purpose: str
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int
    cost_usd: float
    created_at: datetime


class DocumentDetail(BaseModel):
    """Everything about one document, for the detail view."""

    id: uuid.UUID
    file_hash: str
    filename: str
    media_type: str
    size_bytes: int
    status: DocumentStatus
    status_reason: str | None = None
    doc_kind: DocumentKind | None = None
    lane: Lane | None = None
    page_count: int | None = None
    latency_ms: int | None = None
    cost_usd: float = 0.0
    llm_mode: str | None = None
    created_at: datetime
    updated_at: datetime
    extraction: ExtractionOut | None = None
    anomalies: list[AnomalyOut] = Field(default_factory=list)
    audit: list[AuditEntryOut] = Field(default_factory=list)
    traces: list[TraceOut] = Field(default_factory=list)


class ResolveAnomalyRequest(BaseModel):
    """Approve/Reject from the review queue — writes back to Postgres."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class VendorSpend(BaseModel):
    vendor: str
    total: float
    invoice_count: int
    currency: str | None = None


class StatsResponse(BaseModel):
    """`GET /v1/stats` — the KPI cards and the vendor-spend chart."""

    documents_total: int
    documents_processed: int
    documents_needs_review: int
    documents_failed: int
    anomalies_open: int
    anomalies_total: int
    avg_latency_ms: float
    p95_latency_ms: float
    est_cost_usd: float
    avg_cost_per_document_usd: float
    llm_mode: str
    router_model: str
    extractor_model: str
    vendor_spend: list[VendorSpend] = Field(default_factory=list)
    status_breakdown: dict[str, int] = Field(default_factory=dict)
    severity_breakdown: dict[str, int] = Field(default_factory=dict)
    anomaly_type_breakdown: dict[str, int] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: Literal["up", "down"]
    llm_mode: str
    langfuse: Literal["enabled", "disabled"]
