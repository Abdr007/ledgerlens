"""The routing gate — Claude Haiku 4.5 (spec §2, stage 2).

*"Classifies: digital-text PDF vs scanned/photo, and doc type. Cheap model for
cheap decisions = cost-aware model routing."*

Division of labour between the model and the machine:

* **Document type** (invoice / receipt / contract) is a judgement call, so Haiku
  makes it — a fraction of Sonnet's price for a decision that needs no vision.
* **Lane** (text vs vision) is *not* a judgement call. Whether a PDF carries a
  machine-readable text layer is a fact PyMuPDF already measured for free. The
  model's opinion is recorded, but the measurement decides, because routing a
  text-less PDF into the free text lane guarantees an empty extraction.

That split is the whole cost story: a digital PDF never reaches a vision model.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.claude import ClaudeClient, ImageBlock, LlmRequest, LlmUsage
from app.core.files import MediaType
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import DocumentKind, Lane
from app.models.schemas import RoutingDecision
from app.pipeline import textlane
from app.pipeline.prompts import (
    CLASSIFY_TOOL,
    ROUTER_SYSTEM_PROMPT,
    build_routing_user_prompt,
)

logger = get_logger(__name__)

_ROUTER_MAX_TOKENS = 512


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """The routing decision plus everything downstream stages need."""

    decision: RoutingDecision
    analysis: textlane.PdfAnalysis | None
    page_count: int
    usages: tuple[LlmUsage, ...]
    model_lane_opinion: Lane | None
    lane_overridden: bool


async def route_document(
    *,
    client: ClaudeClient,
    settings: Settings,
    data: bytes,
    media_type: MediaType,
    filename: str,
) -> RoutingResult:
    """Classify the upload and choose the cheapest lane that can actually read it."""
    analysis: textlane.PdfAnalysis | None = None
    images: tuple[ImageBlock, ...] = ()
    text_sample = ""
    extracted_chars = 0

    if media_type is MediaType.PDF:
        # Free, instant, zero tokens — and it settles the lane question outright.
        analysis = textlane.analyse_pdf(data)
        page_count = analysis.page_count
        text_sample = analysis.text
        extracted_chars = len(analysis.text)
        mechanical_lane = Lane.TEXT if analysis.has_text_layer else Lane.VISION
    else:
        # A photograph has no text layer by definition.
        page_count = 1
        mechanical_lane = Lane.VISION
        # Haiku is multimodal, so let it see the image to classify the doc type
        # rather than guessing from a filename.
        images = (ImageBlock(media_type=media_type, data_b64=textlane.encode_image(data)),)

    user_prompt = build_routing_user_prompt(
        filename=filename,
        media_type=str(media_type),
        page_count=page_count,
        extracted_chars=extracted_chars,
        text_sample=text_sample,
    )

    result = await client.invoke_tool(
        LlmRequest(
            model=settings.model_router,
            system=ROUTER_SYSTEM_PROMPT,
            user_text=user_prompt,
            tool=CLASSIFY_TOOL,
            max_tokens=_ROUTER_MAX_TOKENS,
            images=images,
            purpose="route",
        )
    )

    decision = RoutingDecision.model_validate(result.payload)
    model_lane = decision.lane
    lane_overridden = model_lane is not mechanical_lane

    if lane_overridden:
        logger.info(
            "router_lane_overridden_by_measurement",
            extra={
                "document_filename": filename,
                "model_lane": str(model_lane),
                "measured_lane": str(mechanical_lane),
                "extracted_chars": extracted_chars,
                "pages": page_count,
            },
        )

    decision = decision.model_copy(update={"lane": mechanical_lane})
    if decision.doc_kind is DocumentKind.UNKNOWN and not decision.reason:
        decision = decision.model_copy(
            update={"reason": "Document type could not be determined with confidence."}
        )

    return RoutingResult(
        decision=decision,
        analysis=analysis,
        page_count=page_count,
        usages=(result.usage,),
        model_lane_opinion=model_lane,
        lane_overridden=lane_overridden,
    )
