"""Structured extraction with self-correction (spec §2, stage 4).

*"Schema-forced output ... Failed validation → error fed back → max 2
self-correction retries."*

The loop is deliberately narrow:

1. Call the forced tool. The response is JSON by construction, not by parsing prose.
2. Parse it into `InvoiceExtraction`. A schema violation (wrong type, invented
   field, unparseable date) is a *repairable* error.
3. Run the deterministic validator. A failing rule is also repairable — the
   feedback names the rule and the numbers, so the model re-reads the page instead
   of guessing.
4. Out of budget? Keep the best-effort extraction and let the document route to
   `NEEDS_REVIEW`. Never raise, never invent, never auto-commit.

A document whose printed arithmetic is genuinely wrong will exhaust the repair
budget and land in the review queue. That is the correct outcome, not a bug: it is
exactly the case the product exists to surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.claude import ClaudeClient, ImageBlock, LlmRequest, LlmUsage
from app.core.files import MediaType
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import Lane
from app.models.schemas import InvoiceExtraction, ValidationReport
from app.pipeline import textlane
from app.pipeline.prompts import (
    EXTRACT_TOOL,
    EXTRACTOR_SYSTEM_PROMPT,
    build_extraction_user_prompt,
    build_repair_user_prompt,
)
from app.pipeline.validate import validate_extraction

logger = get_logger(__name__)

_PAYLOAD_ECHO_LIMIT = 1_800


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One pass through the loop, for the audit trail."""

    attempt: int
    kind: str  # "initial" | "repair"
    schema_valid: bool
    rules_passed: bool
    problem: str | None


@dataclass(slots=True)
class ExtractionOutcome:
    """Final extraction plus everything the ledger and the audit trail need."""

    extraction: InvoiceExtraction
    report: ValidationReport
    raw_payload: dict[str, Any]
    repair_attempts: int
    model: str
    lane: Lane
    usages: tuple[LlmUsage, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    document_text: str | None = None
    failure_note: str | None = field(default=None)
    #: Vision lane only: how many pages were actually shown to the model, and how
    #: many the document has. The renderer caps at `_MAX_VISION_PAGES`, so a long
    #: scan is read from its opening pages and the rest is never seen. `None` on
    #: the text lane, where PyMuPDF reads every page.
    pages_read: int | None = None
    pages_total: int | None = None

    @property
    def pages_unread(self) -> int:
        """Pages the model was never shown. Zero unless the vision lane truncated."""
        if self.pages_read is None or self.pages_total is None:
            return 0
        return max(0, self.pages_total - self.pages_read)


def _format_schema_error(error: ValidationError) -> str:
    """Turn a Pydantic error into feedback a model can act on."""
    lines: list[str] = []
    for issue in error.errors()[:6]:
        location = ".".join(str(part) for part in issue["loc"]) or "(root)"
        lines.append(f"- `{location}`: {issue['msg']}")
    return "The tool input did not match the required schema:\n" + "\n".join(lines)


def _echo(payload: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(payload, indent=2, default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        rendered = str(payload)
    if len(rendered) <= _PAYLOAD_ECHO_LIMIT:
        return rendered
    return rendered[:_PAYLOAD_ECHO_LIMIT] + "\n… (truncated)"


def _build_vision_images(data: bytes, media_type: MediaType) -> tuple[tuple[ImageBlock, ...], int]:
    """Pages the vision lane should look at, and how many the document has.

    The renderer caps the number of pages it rasterises, so the second element is
    what makes the gap between "read" and "present" visible to the caller instead
    of silently disappearing.
    """
    if media_type is MediaType.PDF:
        rendered = textlane.render_pdf_pages(data)
        images = tuple(
            ImageBlock(media_type=MediaType.PNG, data_b64=page.data_b64) for page in rendered
        )
        return images, textlane.page_count_of(data, media_type)
    return (ImageBlock(media_type=media_type, data_b64=textlane.encode_image(data)),), 1


async def extract_document(
    *,
    client: ClaudeClient,
    settings: Settings,
    data: bytes,
    media_type: MediaType,
    filename: str,
    lane: Lane,
    analysis: textlane.PdfAnalysis | None,
) -> ExtractionOutcome:
    """Run the forced-tool extraction with up to `extraction_max_repair_attempts` repairs."""
    document_text: str | None = None
    images: tuple[ImageBlock, ...] = ()
    pages_read: int | None = None
    pages_total: int | None = None

    if lane is Lane.TEXT:
        # PyMuPDF already read this document for free; no image tokens are spent.
        document_text = analysis.text if analysis is not None else textlane.analyse_pdf(data).text
    else:
        images, pages_total = _build_vision_images(data, media_type)
        pages_read = len(images)

    system_prompt = EXTRACTOR_SYSTEM_PROMPT
    user_prompt = build_extraction_user_prompt(
        document_text=document_text, filename=filename, has_images=bool(images)
    )

    max_repairs = max(0, settings.extraction_max_repair_attempts)
    usages: list[LlmUsage] = []
    attempts: list[AttemptRecord] = []

    best_extraction: InvoiceExtraction | None = None
    best_report: ValidationReport | None = None
    best_payload: dict[str, Any] = {}
    last_problem: str | None = None

    for repair_index in range(max_repairs + 1):
        result = await client.invoke_tool(
            LlmRequest(
                model=settings.model_extractor,
                system=system_prompt,
                user_text=user_prompt,
                tool=EXTRACT_TOOL,
                max_tokens=settings.llm_max_output_tokens,
                images=images,
                purpose="extract" if repair_index == 0 else "extract_repair",
                disable_thinking=True,
            )
        )
        usages.append(result.usage)
        payload = result.payload

        try:
            extraction = InvoiceExtraction.model_validate(payload)
        except ValidationError as exc:
            problem = _format_schema_error(exc)
            attempts.append(
                AttemptRecord(
                    attempt=repair_index + 1,
                    kind="initial" if repair_index == 0 else "repair",
                    schema_valid=False,
                    rules_passed=False,
                    problem=problem,
                )
            )
            last_problem = problem
        else:
            report = validate_extraction(
                extraction,
                vat_rate=settings.vat_rate,
                money_tolerance=settings.money_tolerance,
            )
            # Keep the *best* schema-valid result, not merely the newest. A later
            # attempt has more information, but it can still come back worse;
            # accepting it unconditionally would let a good extraction be replaced
            # by an emptier one and silently lose fields.
            if best_report is None or len(report.failures) <= len(best_report.failures):
                best_extraction, best_report, best_payload = extraction, report, payload
            attempts.append(
                AttemptRecord(
                    attempt=repair_index + 1,
                    kind="initial" if repair_index == 0 else "repair",
                    schema_valid=True,
                    rules_passed=report.passed,
                    problem=None if report.passed else report.failure_summary(),
                )
            )
            if report.passed:
                return ExtractionOutcome(
                    extraction=extraction,
                    report=report,
                    raw_payload=payload,
                    repair_attempts=repair_index,
                    model=settings.model_extractor,
                    lane=lane,
                    pages_read=pages_read,
                    pages_total=pages_total,
                    usages=tuple(usages),
                    attempts=tuple(attempts),
                    document_text=document_text,
                )
            last_problem = report.failure_summary()

        if repair_index == max_repairs:
            break

        user_prompt = build_repair_user_prompt(
            previous_payload=_echo(payload),
            problem=last_problem or "The extraction was rejected.",
            attempt=repair_index + 1,
            max_attempts=max_repairs,
            document_text=document_text,
            filename=filename,
            has_images=bool(images),
        )
        logger.info(
            "extraction_repair_scheduled",
            extra={
                "document_filename": filename,
                "attempt": repair_index + 1,
                "max_attempts": max_repairs,
                "lane": str(lane),
            },
        )

    if best_extraction is not None and best_report is not None:
        # Schema-valid but the deterministic rules still object: hand it to a human
        # with the extraction intact so they can see exactly what the page says.
        return ExtractionOutcome(
            extraction=best_extraction,
            report=best_report,
            raw_payload=best_payload,
            repair_attempts=max_repairs,
            model=settings.model_extractor,
            lane=lane,
            pages_read=pages_read,
            pages_total=pages_total,
            usages=tuple(usages),
            attempts=tuple(attempts),
            document_text=document_text,
            failure_note=last_problem,
        )

    # The model never produced schema-valid output. Fall back to an empty
    # extraction so the presence rules fail cleanly and the document is reviewed,
    # rather than fabricating fields nobody can trace to the page.
    empty = InvoiceExtraction()
    return ExtractionOutcome(
        extraction=empty,
        report=validate_extraction(
            empty, vat_rate=settings.vat_rate, money_tolerance=settings.money_tolerance
        ),
        raw_payload={},
        repair_attempts=max_repairs,
        model=settings.model_extractor,
        lane=lane,
        pages_read=pages_read,
        pages_total=pages_total,
        usages=tuple(usages),
        attempts=tuple(attempts),
        document_text=document_text,
        failure_note=(
            "The model did not return schema-valid output within the repair budget: "
            f"{last_problem or 'unknown schema error'}"
        ),
    )
