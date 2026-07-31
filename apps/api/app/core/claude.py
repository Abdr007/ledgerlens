"""Claude access layer.

Two interchangeable implementations sit behind one `ClaudeClient` protocol:

* `LiveClaudeClient`  — the Anthropic SDK. Schema-forced output via **tool use**
  with `tool_choice` pinned to a single tool, so the model cannot reply in prose.
* `OfflineClaudeClient` — a deterministic, network-free substitute used by CI, by
  the offline test suite, and before an API key is available. It answers the same
  tool contracts from the document's *real* extracted text, so the surrounding
  pipeline (hashing, routing, validation, anomaly screening, persistence) is
  exercised for real rather than mocked away.

Which one is live is decided once, by `Settings.use_live_llm`, and every call
records its mode in `LlmUsage.mode` so a trace is never ambiguous about whether a
number came from Claude or from the offline baseline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from app.core.errors import UpstreamUnavailableError
from app.core.files import MediaType
from app.core.logging import get_logger
from app.core.retry import RetriesExhaustedError, RetryPolicy, run_with_retry
from app.core.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only; the SDK is imported lazily
    from anthropic import Omit
    from anthropic.types import (
        ContentBlockParam,
        Message,
        MessageParam,
        ThinkingConfigDisabledParam,
        ToolChoiceToolParam,
        ToolParam,
    )

# The SDK narrows image media types to a Literal; map our enum onto it explicitly
# so a new MediaType member cannot silently reach the vision lane untyped.
_IMAGE_MEDIA_TYPES: Final[dict[MediaType, Literal["image/png", "image/jpeg"]]] = {
    MediaType.PNG: "image/png",
    MediaType.JPEG: "image/jpeg",
}

logger = get_logger(__name__)

# USD per million tokens (input, output). Source: Anthropic pricing, cached 2026-07.
_PRICING: Final[dict[str, tuple[float, float]]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_FALLBACK_PRICING: Final[tuple[float, float]] = (3.00, 15.00)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of a single call, in USD, from the cached price table."""
    price_in, price_out = _PRICING.get(model, _FALLBACK_PRICING)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """A base64 image to hand to the vision lane."""

    media_type: MediaType
    data_b64: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A single forced tool — this is how the schema is enforced on the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LlmRequest:
    model: str
    system: str
    user_text: str
    tool: ToolSpec
    max_tokens: int
    images: tuple[ImageBlock, ...] = ()
    purpose: str = "unspecified"
    # Sonnet 5 runs adaptive thinking whenever `thinking` is omitted, where Sonnet 4.6
    # ran none. Set this on lanes that want the older, deterministic behaviour; see
    # `LiveClaudeClient.invoke_tool` for why extraction does.
    disable_thinking: bool = False


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """Everything Langfuse and the cost KPI need from one call."""

    model: str
    mode: str  # "live" | "offline"
    purpose: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int
    cost_usd: float
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LlmResult:
    payload: dict[str, Any]
    usage: LlmUsage


@runtime_checkable
class ClaudeClient(Protocol):
    """The single seam between the pipeline and any model provider."""

    @property
    def mode(self) -> str: ...

    async def invoke_tool(self, request: LlmRequest) -> LlmResult: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


class LiveClaudeClient:
    """Anthropic-backed client. Forced tool use, no prose path."""

    def __init__(self, settings: Settings) -> None:
        import anthropic  # imported lazily so offline installs need no credentials

        if settings.anthropic_api_key is None:
            msg = "ANTHROPIC_API_KEY is required when llm_mode='live'."
            raise RuntimeError(msg)

        self._anthropic = anthropic
        # max_retries=0: our RetryPolicy is the single source of truth, otherwise
        # the SDK's own retries multiply with ours (3 x 3 = 9 real attempts).
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=0,
            timeout=settings.llm_timeout_s,
        )
        self._policy = RetryPolicy(
            attempts=settings.llm_max_attempts,
            timeout_s=settings.llm_timeout_s,
            retry_on=(
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                TimeoutError,
                ConnectionError,
            ),
        )

    @property
    def mode(self) -> str:
        return "live"

    def _build_content(self, request: LlmRequest) -> list[ContentBlockParam]:
        """Images first, then the instruction text — the order Claude reads best."""
        blocks: list[ContentBlockParam] = []
        for image in request.images:
            media_type = _IMAGE_MEDIA_TYPES.get(image.media_type)
            if media_type is None:
                # PDFs never reach the vision lane as images; they are rasterised
                # to PNG upstream. Anything else is a programming error.
                msg = f"{image.media_type} cannot be sent as an image block"
                raise ValueError(msg)
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image.data_b64,
                    },
                }
            )
        blocks.append({"type": "text", "text": request.user_text})
        return blocks

    async def invoke_tool(self, request: LlmRequest) -> LlmResult:
        started = time.perf_counter()

        tools: list[ToolParam] = [
            {
                "name": request.tool.name,
                "description": request.tool.description,
                "input_schema": request.tool.input_schema,
            }
        ]
        # Pinning tool_choice to this one tool is what makes the output schema-forced.
        tool_choice: ToolChoiceToolParam = {"type": "tool", "name": request.tool.name}
        messages: list[MessageParam] = [{"role": "user", "content": self._build_content(request)}]

        # Omitting `thinking` means "no thinking" on Sonnet 4.6 but "adaptive thinking"
        # on Sonnet 5, so the extractor asks for it off explicitly. Two reasons:
        # `max_tokens` caps thinking and output *together*, so a long think can starve
        # the tool_use block and drop us into the "no structured output" branch below;
        # and thinking bills as output tokens, which moves the cost-per-document KPI for
        # no accuracy gain on what is a transcription task — the Decimal layer, not the
        # model, is what checks the arithmetic. The usual "models reach for tools less
        # with thinking off" caveat does not apply here: `tool_choice` is pinned to one
        # tool, so the call is forced either way.
        thinking: ThinkingConfigDisabledParam | Omit = (
            {"type": "disabled"} if request.disable_thinking else self._anthropic.omit
        )

        async def _call() -> Message:
            return await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
                thinking=thinking,
            )

        try:
            response, outcome = await run_with_retry(
                f"claude.{request.purpose}", _call, self._policy
            )
        except RetriesExhaustedError as exc:
            raise UpstreamUnavailableError(
                "The extraction model is temporarily unavailable.",
                details={"operation": exc.operation, "attempts": exc.attempts},
            ) from exc

        payload: dict[str, Any] | None = None
        for block in response.content:
            # Comparing the literal discriminator narrows the content-block union.
            if block.type == "tool_use" and block.name == request.tool.name:
                raw_input = block.input
                payload = dict(raw_input) if isinstance(raw_input, dict) else {}
                break

        if payload is None:
            # tool_choice pins a single tool, so this means a refusal or a truncated
            # response — surface it rather than inventing an empty extraction.
            raise UpstreamUnavailableError(
                "Model returned no structured output.",
                details={"stop_reason": response.stop_reason, "purpose": request.purpose},
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        input_tokens = int(response.usage.input_tokens)
        output_tokens = int(response.usage.output_tokens)
        return LlmResult(
            payload=payload,
            usage=LlmUsage(
                model=request.model,
                mode="live",
                purpose=request.purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                attempts=outcome.attempts_used,
                cost_usd=estimate_cost_usd(request.model, input_tokens, output_tokens),
                stop_reason=response.stop_reason,
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OfflineClaudeClient:
    """Deterministic, network-free stand-in for Claude.

    It fulfils the same tool contracts by parsing the document's real extracted
    text (see `app.pipeline.offline`). Token counts are estimated so that latency
    and cost dashboards stay populated, and every usage record is stamped
    `mode="offline"` so no number is ever mistaken for a live measurement.
    """

    handlers: dict[str, Any] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return "offline"

    def register(self, tool_name: str, handler: Any) -> None:
        self.handlers[tool_name] = handler

    async def invoke_tool(self, request: LlmRequest) -> LlmResult:
        started = time.perf_counter()
        handler = self.handlers.get(request.tool.name)
        if handler is None:
            raise UpstreamUnavailableError(
                "No offline handler is registered for this tool.",
                details={"tool": request.tool.name},
            )
        payload: dict[str, Any] = handler(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        # ~4 characters per token is the usual English approximation; it is only
        # used to keep the cost panel meaningful in offline mode.
        input_tokens = max(1, len(request.user_text) // 4)
        output_tokens = max(1, len(str(payload)) // 4)
        return LlmResult(
            payload=payload,
            usage=LlmUsage(
                model=f"offline:{request.model}",
                mode="offline",
                purpose=request.purpose,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                attempts=1,
                cost_usd=0.0,
                stop_reason="tool_use",
            ),
        )

    async def aclose(self) -> None:
        return None
