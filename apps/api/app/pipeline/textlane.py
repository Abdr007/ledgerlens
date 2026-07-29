"""The text lane — PyMuPDF (spec §2, stage 3a).

*"PyMuPDF extracts embedded text. Free, instant, zero tokens."*

This module also owns rasterisation for the vision lane: a scanned PDF has no
usable text layer, so its pages are rendered to PNG here before Claude sees them.
Rendering is capped at a target long edge, which keeps image tokens (and cost)
predictable instead of scaling with whatever DPI the scanner happened to use.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

import pymupdf

from app.core.errors import MalformedFileError
from app.core.files import MediaType
from app.core.logging import get_logger

logger = get_logger(__name__)

# Claude resizes images above ~1568px on the long edge; rendering larger than that
# costs tokens for detail the model never sees.
_TARGET_LONG_EDGE_PX: Final = 1_500
_MAX_VISION_PAGES: Final = 3
_PDF_POINTS_PER_INCH: Final = 72.0

# A page with fewer than this many extractable characters is treated as a scan.
# Real digital invoices carry hundreds; a scanned page yields 0-30 characters of
# stray OCR noise, so the boundary is wide and unambiguous.
MIN_CHARS_PER_PAGE_FOR_TEXT_LANE: Final = 60

# Words whose tops fall within this many points belong to the same visual row.
# Wide enough to keep a bold 11pt total beside its 9pt label, narrow enough not
# to merge adjacent table rows (which sit ~5.6pt apart in a dense invoice).
# --- Resource-exhaustion guards ------------------------------------------
# A 10 MB PDF can legitimately declare tens of thousands of pages, or a single
# page the size of a city block. Both render into gigabytes of pixels and take
# the process down long before any size check on the *file* would notice.
MAX_PDF_PAGES: Final = 100
MAX_RENDER_PIXELS: Final = 40_000_000  # ~40 MP per page
# The extracted text is embedded in a prompt; an unbounded page would blow the
# context window and the token bill with it.
MAX_DOCUMENT_TEXT_CHARS: Final = 120_000

_ROW_BAND_PT: Final = 3.2
# Approximate advance width of a space in the body font, used to turn a
# horizontal gap back into padding.
_SPACE_WIDTH_PT: Final = 4.0
_MAX_GAP_SPACES: Final = 12


@dataclass(frozen=True, slots=True)
class PdfAnalysis:
    """What the text lane learned about a PDF without spending a token."""

    page_count: int
    text: str
    chars_per_page: float
    has_text_layer: bool


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One page rasterised for the vision lane."""

    page_index: int
    png_bytes: bytes
    width: int
    height: int

    @property
    def data_b64(self) -> str:
        return base64.standard_b64encode(self.png_bytes).decode("ascii")


def _layout_text(page: pymupdf.Page) -> str:
    """Reconstruct the page as rows, preserving the visual column layout.

    A PDF stores *positioned glyphs*, not lines: `get_text("text")` emits every
    drawing operation separately, so an invoice table comes out as
    `description / qty / unit price / amount` on four consecutive lines. Any
    reader — the rule-based parser and Claude alike — then has to guess which
    fragments belonged to the same row.

    Grouping words into rows by their vertical position and padding the
    horizontal gaps restores the table a human sees, which is what both readers
    are actually good at.
    """
    words = page.get_text("words")
    if not words:
        return ""

    # (x0, y0, x1, y1, word, block, line, word_no)
    ordered = sorted(words, key=lambda word: (word[1], word[0]))

    rows: list[list[tuple[float, float, str]]] = []
    row_top = ordered[0][1]
    current: list[tuple[float, float, str]] = []
    for x0, y0, x1, _y1, text, *_ in ordered:
        if current and (y0 - row_top) > _ROW_BAND_PT:
            rows.append(current)
            current = []
            row_top = y0
        current.append((x0, x1, text))
    if current:
        rows.append(current)

    lines: list[str] = []
    for row in rows:
        parts: list[str] = []
        previous_right: float | None = None
        for x0, x1, text in sorted(row, key=lambda item: item[0]):
            if previous_right is not None:
                gap = x0 - previous_right
                # Pad proportionally so column boundaries survive as whitespace.
                spaces = max(1, min(int(gap / _SPACE_WIDTH_PT), _MAX_GAP_SPACES))
                parts.append(" " * spaces)
            parts.append(text)
            previous_right = x1
        lines.append("".join(parts))
    return "\n".join(lines)


def analyse_pdf(data: bytes) -> PdfAnalysis:
    """Extract embedded text and decide whether a real text layer exists."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            page_count = document.page_count
            if page_count == 0:
                raise MalformedFileError("PDF contains no pages.")
            if page_count > MAX_PDF_PAGES:
                raise MalformedFileError(
                    f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}.",
                    details={"pages": page_count, "max_pages": MAX_PDF_PAGES},
                )
            chunks = [_layout_text(page) for page in document]
    except MalformedFileError:
        raise
    except Exception as exc:  # any parser failure means the upload is unusable
        raise MalformedFileError(
            "The PDF could not be parsed.", details={"reason": type(exc).__name__}
        ) from exc

    text = "\n".join(chunks).strip()
    if len(text) > MAX_DOCUMENT_TEXT_CHARS:
        logger.warning(
            "document_text_truncated",
            extra={"characters": len(text), "limit": MAX_DOCUMENT_TEXT_CHARS},
        )
        text = text[:MAX_DOCUMENT_TEXT_CHARS]
    chars_per_page = len(text) / page_count if page_count else 0.0
    return PdfAnalysis(
        page_count=page_count,
        text=text,
        chars_per_page=chars_per_page,
        has_text_layer=chars_per_page >= MIN_CHARS_PER_PAGE_FOR_TEXT_LANE,
    )


def render_pdf_pages(data: bytes, *, max_pages: int = _MAX_VISION_PAGES) -> list[RenderedPage]:
    """Rasterise the first `max_pages` pages to PNG for the vision lane."""
    pages: list[RenderedPage] = []
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            for index in range(min(document.page_count, max_pages)):
                page = document[index]
                rect = page.rect
                long_edge_pt = max(rect.width, rect.height) or _PDF_POINTS_PER_INCH
                # Scale so the long edge lands on the target, never upscaling a
                # page that is already larger than we need.
                zoom = min(_TARGET_LONG_EDGE_PX / long_edge_pt, 4.0)
                # Guard against a page whose declared media box is enormous: the
                # zoom is capped by the long edge, but the *short* edge can still
                # multiply out to a pixmap that exhausts memory.
                projected = (rect.width * zoom) * (rect.height * zoom)
                if projected > MAX_RENDER_PIXELS:
                    zoom *= (MAX_RENDER_PIXELS / projected) ** 0.5
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                pages.append(
                    RenderedPage(
                        page_index=index,
                        png_bytes=bytes(pixmap.tobytes("png")),
                        width=pixmap.width,
                        height=pixmap.height,
                    )
                )
    except Exception as exc:  # any render failure means the upload is unusable
        raise MalformedFileError(
            "The PDF could not be rendered.", details={"reason": type(exc).__name__}
        ) from exc

    if not pages:
        raise MalformedFileError("PDF contains no renderable pages.")
    return pages


def page_count_of(data: bytes, media_type: MediaType) -> int:
    """Page count for a PDF; images are a single page by definition."""
    if media_type is not MediaType.PDF:
        return 1
    return analyse_pdf(data).page_count


def encode_image(data: bytes) -> str:
    """Base64 an uploaded image for a Claude image block."""
    return base64.standard_b64encode(data).decode("ascii")
