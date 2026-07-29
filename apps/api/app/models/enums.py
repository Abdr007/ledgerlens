"""Domain enums shared by the ORM, the API schemas and the UI.

These string values are part of the public API contract — the web client switches
on them directly, so they are stable and lowercase-free where the spec names them.
"""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    """Spec §7: `PENDING → PROCESSING → DONE / NEEDS_REVIEW`, with a terminal
    `FAILED` for jobs that exhausted their retry budget (spec §9, check 2)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {DocumentStatus.DONE, DocumentStatus.NEEDS_REVIEW, DocumentStatus.FAILED}
)

# The only transitions the state machine will perform. Anything else is a bug and
# is rejected by `InvalidStateTransitionError`.
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset({DocumentStatus.PROCESSING, DocumentStatus.FAILED}),
    DocumentStatus.PROCESSING: frozenset(
        {DocumentStatus.DONE, DocumentStatus.NEEDS_REVIEW, DocumentStatus.FAILED}
    ),
    DocumentStatus.DONE: frozenset(),
    DocumentStatus.NEEDS_REVIEW: frozenset(),
    DocumentStatus.FAILED: frozenset(),
}


class DocumentKind(StrEnum):
    """Doc type decided by the routing gate (spec §2, stage 2)."""

    INVOICE = "invoice"
    RECEIPT = "receipt"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


class Lane(StrEnum):
    """Which extraction lane the router selected."""

    TEXT = "text"  # PyMuPDF embedded text — free, instant, zero tokens
    VISION = "vision"  # Claude Sonnet 4.6 vision — scans, tables, handwriting


class PipelineStage(StrEnum):
    """The six stages the UI animates (spec §6)."""

    INGEST = "ingest"
    ROUTE = "route"
    EXTRACT = "extract"
    VALIDATE = "validate"
    SCREEN = "screen"
    LEDGER = "ledger"


PIPELINE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.INGEST,
    PipelineStage.ROUTE,
    PipelineStage.EXTRACT,
    PipelineStage.VALIDATE,
    PipelineStage.SCREEN,
    PipelineStage.LEDGER,
)


class StageState(StrEnum):
    """Per-stage state the pipeline visual renders."""

    PENDING = "pending"
    ACTIVE = "active"
    PASSED = "passed"
    FLAGGED = "flagged"
    FAILED = "failed"


class AnomalyType(StrEnum):
    """The four detectors required by spec §2, stage 6."""

    DUPLICATE = "duplicate"
    AMOUNT_ZSCORE = "amount_zscore"
    TERM_DRIFT = "term_drift"
    ROUND_NUMBER = "round_number"


class AnomalySeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 1, "MEDIUM": 2, "HIGH": 3}[self.value]


class AnomalyStatus(StrEnum):
    """Review-queue state, written back from the UI's Approve/Reject buttons."""

    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AuditEvent(StrEnum):
    """Append-only audit-log event names (spec §2, stage 7)."""

    DOCUMENT_RECEIVED = "document.received"
    DOCUMENT_DEDUPLICATED = "document.deduplicated"
    STATUS_CHANGED = "document.status_changed"
    ROUTED = "pipeline.routed"
    TEXT_EXTRACTED = "pipeline.text_extracted"
    EXTRACTION_ATTEMPTED = "pipeline.extraction_attempted"
    EXTRACTION_REPAIRED = "pipeline.extraction_repaired"
    EXTRACTION_SUCCEEDED = "pipeline.extraction_succeeded"
    VALIDATION_PASSED = "pipeline.validation_passed"
    VALIDATION_FAILED = "pipeline.validation_failed"
    SCREENING_COMPLETED = "pipeline.screened"
    LEDGER_COMMITTED = "pipeline.ledger_committed"
    ANOMALY_RAISED = "anomaly.raised"
    ANOMALY_RESOLVED = "anomaly.resolved"
    JOB_FAILED = "pipeline.job_failed"
