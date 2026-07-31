"""Liveness and readiness.

`/health` reports `degraded` rather than failing when the database *goes away*,
so a platform health check can distinguish "the process is up but its dependency
is down" from "the process is gone".

The wording is deliberate. This only covers a database that was reachable at boot
and later was not. A process that starts with an unreachable database never gets
here at all: `lifespan` awaits `wait_for_database` before serving anything, and
exits when that exhausts its retries. Failing fast on a misconfiguration beats
serving a service that cannot do its job — but it does mean `degraded` is a
report about a running system, never about a broken deployment.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tracing import get_tracer
from app.deps import get_app_settings, get_claude_client
from app.models.api import HealthResponse

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

LangfuseState = Literal["enabled", "disabled", "unavailable"]


def langfuse_state(*, tracer_mode: str, configured: bool) -> LangfuseState:
    """Report the exporter that exists, not the one that was asked for.

    `get_tracer` falls back to the local sink when the Langfuse client cannot be
    constructed — a bad key, an unreachable host, a missing dependency — and that
    fallback is deliberately non-fatal, because losing telemetry should not take
    the service down with it. Reporting `settings.langfuse_enabled` here therefore
    answered a different question than the one being asked: it said the keys were
    present, and a deployment ran with tracing silently off while `/health`
    cheerfully said `enabled`. Configuration is an intention; this is an outcome.
    """
    if tracer_mode == "langfuse":
        return "enabled"
    return "unavailable" if configured else "disabled"


@router.get("/health", response_model=HealthResponse, summary="Service health")
async def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> HealthResponse:
    database: str = "down"
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        database = "up"
    except Exception as exc:  # health must report, never raise
        logger.warning("health_database_unreachable", extra={"error_type": type(exc).__name__})

    return HealthResponse(
        # `status` stays a statement about the database alone. A tracing outage is
        # worth reporting, but it does not stop this service doing its job, and a
        # platform health check should not restart a container over it.
        status="ok" if database == "up" else "degraded",
        version="1.0.0",
        environment=settings.environment,
        database="up" if database == "up" else "down",
        llm_mode=get_claude_client().mode,
        langfuse=langfuse_state(
            tracer_mode=get_tracer().mode, configured=settings.langfuse_enabled
        ),
    )
