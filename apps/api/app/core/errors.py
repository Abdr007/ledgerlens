"""Typed API errors.

Spec §7: *"API returns typed error responses, never stack traces."* Every failure
that reaches a client is one of these, serialised to a stable JSON envelope:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers. Values are part of the API."""

    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    FILE_TOO_LARGE = "file_too_large"
    EMPTY_FILE = "empty_file"
    MALFORMED_FILE = "malformed_file"
    FORBIDDEN_ORIGIN = "forbidden_origin"
    NOT_FOUND = "not_found"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    EXTRACTION_FAILED = "extraction_failed"
    INTERNAL_ERROR = "internal_error"


class LedgerLensError(Exception):
    """Base class for every error the API deliberately surfaces."""

    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": str(self.code), "message": self.message, "details": self.details}}


class UnsupportedMediaTypeError(LedgerLensError):
    status_code = 415
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE


class FileTooLargeError(LedgerLensError):
    status_code = 413
    code = ErrorCode.FILE_TOO_LARGE


class EmptyFileError(LedgerLensError):
    status_code = 400
    code = ErrorCode.EMPTY_FILE


class MalformedFileError(LedgerLensError):
    status_code = 400
    code = ErrorCode.MALFORMED_FILE


class NotFoundError(LedgerLensError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class InvalidStateTransitionError(LedgerLensError):
    status_code = 409
    code = ErrorCode.INVALID_STATE_TRANSITION


class ValidationFailedError(LedgerLensError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR


class UpstreamUnavailableError(LedgerLensError):
    """A dependency (Claude, database) failed after every retry was exhausted."""

    status_code = 503
    code = ErrorCode.UPSTREAM_UNAVAILABLE


class ExtractionFailedError(LedgerLensError):
    """The model could not produce a schema-valid extraction within the retry budget."""

    status_code = 422
    code = ErrorCode.EXTRACTION_FAILED
