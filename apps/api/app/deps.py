"""Dependency wiring.

The model client is built exactly once per process and chosen by configuration:
live Anthropic when a key is present, the deterministic offline engine otherwise.
Nothing downstream knows or cares which one it got — that is the point of the
`ClaudeClient` protocol.
"""

from __future__ import annotations

import functools

from app.core.claude import ClaudeClient, LiveClaudeClient, OfflineClaudeClient
from app.core.logging import get_logger
from app.core.settings import Settings, get_settings
from app.core.tracing import Tracer, get_tracer
from app.pipeline.offline import build_offline_handlers
from app.pipeline.orchestrator import PipelineOrchestrator

logger = get_logger(__name__)


@functools.lru_cache(maxsize=1)
def get_claude_client() -> ClaudeClient:
    """The process-wide model client."""
    settings = get_settings()
    if settings.use_live_llm:
        logger.info(
            "claude_client_live",
            extra={"router": settings.model_router, "extractor": settings.model_extractor},
        )
        return LiveClaudeClient(settings)

    logger.warning(
        "claude_client_offline",
        extra={
            "reason": (
                "llm_mode=stub"
                if settings.llm_mode == "stub"
                else "no ANTHROPIC_API_KEY configured"
            ),
            "detail": (
                "Running the deterministic offline extraction engine. Documents are "
                "processed for real end to end; scans without a text layer will route "
                "to NEEDS_REVIEW until a key is configured."
            ),
        },
    )
    client = OfflineClaudeClient()
    for tool_name, handler in build_offline_handlers().items():
        client.register(tool_name, handler)
    return client


def get_orchestrator() -> PipelineOrchestrator:
    """A pipeline bound to the process-wide client, settings and tracer."""
    return PipelineOrchestrator(
        client=get_claude_client(),
        settings=get_settings(),
        tracer=get_tracer(),
    )


def get_app_settings() -> Settings:
    return get_settings()


def get_app_tracer() -> Tracer:
    return get_tracer()


async def shutdown_clients() -> None:
    """Release the model client's connection pool on shutdown."""
    client = get_claude_client()
    await client.aclose()
    get_claude_client.cache_clear()
