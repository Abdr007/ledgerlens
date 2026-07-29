"""SQLAlchemy 2 ORM tables — the ledger (spec §2, stage 7).

`documents · extractions · anomalies · audit_log · failed_jobs` (+ `llm_traces`
for the observability panel).

Integrity is enforced *in the database*, not just in Python:

* `UNIQUE(file_hash)` on `documents` is what makes ingestion idempotent under
  concurrency — two simultaneous uploads of the same bytes cannot both insert.
* `CHECK` constraints pin every enum column to its legal values.
* `audit_log` is made genuinely append-only by a trigger that raises on
  `UPDATE`/`DELETE` (see `DDL_STATEMENTS`), so history cannot be rewritten even
  by a direct `psql` session.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.models.enums import (
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    DocumentKind,
    DocumentStatus,
    Lane,
)


class Base(DeclarativeBase):
    """Declarative base for every LedgerLens table."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _enum_values(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL literal list for a CHECK constraint."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


MONEY = Numeric(14, 2)


class Document(Base):
    """One ingested file. `file_hash` is the idempotency key."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentStatus.PENDING.value, index=True
    )
    status_reason: Mapped[str | None] = mapped_column(Text)

    doc_kind: Mapped[str | None] = mapped_column(String(16))
    lane: Mapped[str | None] = mapped_column(String(16))
    page_count: Mapped[int | None] = mapped_column(Integer)

    # Rolled up from the pipeline run so the KPI cards need no aggregation joins.
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    llm_mode: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    extraction: Mapped[Extraction | None] = relationship(
        back_populates="document", uselist=False, cascade="all, delete-orphan"
    )
    anomalies: Mapped[list[Anomaly]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_enum_values(DocumentStatus)})", name="ck_documents_status"),
        CheckConstraint(
            f"doc_kind IS NULL OR doc_kind IN ({_enum_values(DocumentKind)})",
            name="ck_documents_doc_kind",
        ),
        CheckConstraint(
            f"lane IS NULL OR lane IN ({_enum_values(Lane)})", name="ck_documents_lane"
        ),
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        Index("ix_documents_status_created", "status", "created_at"),
    )


class Extraction(Base):
    """Schema-validated fields for one document.

    Money and dates are promoted to typed columns so the vendor-spend chart and
    the duplicate detector can query them directly; the model's untouched output
    is retained in `raw_payload` for audit.
    """

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    vendor: Mapped[str | None] = mapped_column(String(256), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    # Calendar dates, not instants: an invoice is dated 2026-03-14 everywhere
    # on earth, so storing a timestamptz would invent a timezone question.
    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date)
    subtotal: Mapped[Decimal | None] = mapped_column(MONEY)
    tax: Mapped[Decimal | None] = mapped_column(MONEY)
    total: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(8))
    payment_terms: Mapped[str | None] = mapped_column(String(128))

    line_items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(String(64))
    lane: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="extraction")

    __table_args__ = (
        CheckConstraint("repair_attempts >= 0", name="ck_extractions_repairs_non_negative"),
        Index("ix_extractions_vendor_issue_date", "vendor", "issue_date"),
    )


class Anomaly(Base):
    """One explainable flag. Every row carries a severity and a plain-English reason."""

    __tablename__ = "anomalies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    anomaly_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # The one-sentence explanation a finance team can act on.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # Stable identity for a finding, so re-screening a document cannot duplicate it.
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AnomalyStatus.OPEN.value, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    document: Mapped[Document] = relationship(back_populates="anomalies")

    __table_args__ = (
        UniqueConstraint("document_id", "fingerprint", name="uq_anomalies_document_fingerprint"),
        CheckConstraint(f"anomaly_type IN ({_enum_values(AnomalyType)})", name="ck_anomalies_type"),
        CheckConstraint(
            f"severity IN ({_enum_values(AnomalySeverity)})", name="ck_anomalies_severity"
        ),
        CheckConstraint(f"status IN ({_enum_values(AnomalyStatus)})", name="ck_anomalies_status"),
        Index("ix_anomalies_status_severity", "status", "severity"),
    )


class AuditLog(Base):
    """Append-only event log. Never updated, never deleted — enforced by trigger."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (Index("ix_audit_log_document_created", "document_id", "created_at"),)


class FailedJob(Base):
    """A pipeline run that exhausted its retry budget, with the reason (spec §7)."""

    __tablename__ = "failed_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class LlmTrace(Base):
    """One model call: tokens, cost, latency, retries (spec §2, observability)."""

    __tablename__ = "llm_traces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# ---------------------------------------------------------------------------
# Raw DDL applied after `metadata.create_all` — guarantees Python cannot bypass.
# ---------------------------------------------------------------------------

DDL_STATEMENTS: tuple[str, ...] = (
    # Append-only audit log: reject any attempt to rewrite history.
    """
    CREATE OR REPLACE FUNCTION ledgerlens_audit_log_is_append_only()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log;",
    """
    CREATE TRIGGER trg_audit_log_no_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION ledgerlens_audit_log_is_append_only();
    """,
    # Case-insensitive vendor lookups for the duplicate detector.
    """
    CREATE INDEX IF NOT EXISTS ix_extractions_vendor_lower
    ON extractions (lower(vendor));
    """,
)
