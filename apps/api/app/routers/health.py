"""Liveness and readiness.

`/health` reports `degraded` rather than failing when the database is
unreachable, so a platform health check can distinguish "the process is up but
its dependency is down" from "the process is gone".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.settings import Settings
from app.deps import get_app_settings, get_claude_client
from app.models.api import HealthResponse

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


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
        status="ok" if database == "up" else "degraded",
        version="1.0.0",
        environment=settings.environment,
        database="up" if database == "up" else "down",
        llm_mode=get_claude_client().mode,
        langfuse="enabled" if settings.langfuse_enabled else "disabled",
    )
