"""Upload hashing and content-type whitelisting.

Spec §7 security bar: *"File type + size whitelist (PDF/PNG/JPG, ≤10MB)."*

The declared `Content-Type` header is attacker-controlled, so it is never trusted
on its own — the media type is decided by **magic bytes**, and a mismatch between
the sniffed type and the declared one is rejected outright rather than silently
resolved in the client's favour.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from app.core.errors import EmptyFileError, FileTooLargeError, UnsupportedMediaTypeError


class MediaType(StrEnum):
    """The only media types LedgerLens will ingest."""

    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"

    @property
    def is_image(self) -> bool:
        return self in {MediaType.PNG, MediaType.JPEG}

    @property
    def extension(self) -> str:
        return {MediaType.PDF: ".pdf", MediaType.PNG: ".png", MediaType.JPEG: ".jpg"}[self]


# (magic prefix, media type). Order matters only for readability; prefixes are distinct.
_MAGIC: Final[tuple[tuple[bytes, MediaType], ...]] = (
    (b"%PDF-", MediaType.PDF),
    (b"\x89PNG\r\n\x1a\n", MediaType.PNG),
    (b"\xff\xd8\xff", MediaType.JPEG),
)

# Declared content types we accept for each sniffed type (browsers vary).
_DECLARED_ALIASES: Final[dict[MediaType, frozenset[str]]] = {
    MediaType.PDF: frozenset({"application/pdf", "application/x-pdf", "application/octet-stream"}),
    MediaType.PNG: frozenset({"image/png", "application/octet-stream"}),
    MediaType.JPEG: frozenset({"image/jpeg", "image/jpg", "application/octet-stream"}),
}

_MIN_SNIFF_BYTES: Final = 8
_MAX_FILENAME_CHARS: Final = 200
# C0/C1 control characters, plus the bidirectional-override codepoints used to
# disguise an extension (e.g. "invoice\u202Egpj.exe" renders as "invoice.jpg").
_UNSAFE_FILENAME_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def sanitise_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to something safe to store and render.

    The name is attacker-controlled and ends up in the database, in log lines and
    in the browser. Three separate problems are closed here: path traversal
    (`../../etc/passwd`), terminal/UI control characters, and bidirectional
    overrides that make a filename render as a different extension than it has.
    """
    if not raw:
        return "document"
    # Take the final path component under both separator conventions.
    candidate = raw.replace("\\", "/")
    candidate = PurePosixPath(candidate).name
    candidate = unicodedata.normalize("NFKC", candidate)
    candidate = _UNSAFE_FILENAME_CHARS.sub("", candidate).strip(" .")
    candidate = candidate[:_MAX_FILENAME_CHARS]
    return candidate or "document"


def sha256_hex(data: bytes) -> str:
    """Content hash used as the idempotency key (spec §2, stage 1)."""
    return hashlib.sha256(data).hexdigest()


def sniff_media_type(data: bytes) -> MediaType | None:
    """Identify the media type from magic bytes, or `None` if unrecognised."""
    if len(data) < _MIN_SNIFF_BYTES:
        return None
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return media_type
    return None


def validate_upload(
    data: bytes,
    *,
    declared_content_type: str | None,
    max_bytes: int,
) -> MediaType:
    """Whitelist an upload by size and true content type.

    Raises:
        EmptyFileError: zero-byte upload.
        FileTooLargeError: exceeds `max_bytes`.
        UnsupportedMediaTypeError: not a PDF/PNG/JPEG, or the declared type
            contradicts the sniffed one.
    """
    if not data:
        raise EmptyFileError("Uploaded file is empty.")

    if len(data) > max_bytes:
        raise FileTooLargeError(
            f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            details={"size_bytes": len(data), "max_bytes": max_bytes},
        )

    media_type = sniff_media_type(data)
    if media_type is None:
        raise UnsupportedMediaTypeError(
            "Only PDF, PNG and JPEG documents are accepted.",
            details={"allowed": [str(m) for m in MediaType]},
        )

    if declared_content_type:
        declared = declared_content_type.split(";", 1)[0].strip().lower()
        if declared and declared not in _DECLARED_ALIASES[media_type]:
            raise UnsupportedMediaTypeError(
                "Declared content type does not match the file's actual contents.",
                details={"declared": declared, "detected": str(media_type)},
            )

    return media_type
