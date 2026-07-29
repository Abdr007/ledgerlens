"""Observability for every LLM call.

Spec §2/§3: *"Langfuse (free cloud): every LLM call traced — tokens, cost,
latency, retries."*

Two sinks, deliberately:

* **Local (always on)** — every call is written to the `llm_traces` table and to
  the structured log. Observability therefore works with zero third-party
  accounts, and the UI can render a real trace timeline out of the box.
* **Langfuse (when keys are present)** — the same records are mirrored to Langfuse
  Cloud as `generation` observations, giving the hosted token/cost/latency view.

Langfuse is imported lazily and every export is failure-isolated: telemetry must
never be able to fail a document.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.claude import LlmUsage
from app.core.logging import get_logger
from app.core.settings import Settings, get_settings

logger = get_logger(__name__)

_PREVIEW_CHARS = 2_000


def _preview(text: str) -> str:
    """Truncate free text before it leaves the process."""
    if len(text) <= _PREVIEW_CHARS:
        return text
    return f"{text[:_PREVIEW_CHARS]}… [truncated {len(text) - _PREVIEW_CHARS} chars]"


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Identifies the document a call belongs to."""

    document_id: str
    file_hash: str
    stage: str


class Tracer(Protocol):
    """Sink for completed LLM calls."""

    def on_llm_call(
        self,
        context: TraceContext,
        usage: LlmUsage,
        *,
        input_preview: str,
        output_preview: str,
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class LocalTracer:
    """Structured-log sink. Always active; the DB row is written by the pipeline."""

    def on_llm_call(
        self,
        context: TraceContext,
        usage: LlmUsage,
        *,
        input_preview: str,
        output_preview: str,
    ) -> None:
        logger.info(
            "llm_call",
            extra={
                "document_id": context.document_id,
                "stage": context.stage,
                "model": usage.model,
                "mode": usage.mode,
                "purpose": usage.purpose,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "latency_ms": usage.latency_ms,
                "attempts": usage.attempts,
                "cost_usd": round(usage.cost_usd, 6),
                "input_chars": len(input_preview),
                "output_chars": len(output_preview),
            },
        )

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class LangfuseTracer:
    """Mirrors calls to Langfuse Cloud as `generation` observations."""

    def __init__(self, settings: Settings) -> None:
        from langfuse import Langfuse  # lazy: only needed when keys are configured

        assert settings.langfuse_public_key is not None
        assert settings.langfuse_secret_key is not None
        self._client: Any = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            host=settings.langfuse_host,
            environment=settings.environment,
            release="ledgerlens@1.0.0",
        )
        self._local = LocalTracer()
        logger.info("langfuse_tracer_enabled", extra={"host": settings.langfuse_host})

    def on_llm_call(
        self,
        context: TraceContext,
        usage: LlmUsage,
        *,
        input_preview: str,
        output_preview: str,
    ) -> None:
        self._local.on_llm_call(
            context, usage, input_preview=input_preview, output_preview=output_preview
        )
        try:
            generation = self._client.start_observation(
                name=f"ledgerlens.{context.stage}",
                as_type="generation",
                model=usage.model,
                input=_preview(input_preview),
                output=_preview(output_preview),
                usage_details={"input": usage.input_tokens, "output": usage.output_tokens},
                cost_details={"total": usage.cost_usd},
                metadata={
                    "document_id": context.document_id,
                    "file_hash": context.file_hash,
                    "stage": context.stage,
                    "purpose": usage.purpose,
                    "mode": usage.mode,
                    "attempts": usage.attempts,
                    "latency_ms": usage.latency_ms,
                    "stop_reason": usage.stop_reason,
                },
            )
            generation.end()
        except Exception as exc:
            logger.warning(
                "langfuse_export_failed",
                extra={"error_type": type(exc).__name__, "stage": context.stage},
            )

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            logger.warning("langfuse_flush_failed", extra={"error_type": type(exc).__name__})

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception as exc:
            logger.warning("langfuse_shutdown_failed", extra={"error_type": type(exc).__name__})


@functools.lru_cache(maxsize=1)
def get_tracer() -> Tracer:
    """Process-wide tracer: Langfuse when configured, local-only otherwise."""
    settings = get_settings()
    if settings.langfuse_enabled:
        try:
            return LangfuseTracer(settings)
        except Exception as exc:
            logger.warning(
                "langfuse_init_failed_falling_back_to_local",
                extra={"error_type": type(exc).__name__},
            )
    return LocalTracer()


def shutdown_tracer() -> None:
    """Flush any buffered telemetry on application shutdown."""
    tracer = get_tracer()
    tracer.flush()
    tracer.shutdown()
    get_tracer.cache_clear()
