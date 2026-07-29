"""Async SQLAlchemy engine, session factory and transaction helpers.

Spec §7: *"all multi-step writes inside DB transactions"*. The only sanctioned way
to touch the database is `transaction()` (write) or `session_scope()` (read), so a
half-applied pipeline stage cannot be committed.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logging import get_logger
from app.core.settings import Settings, get_settings

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _connect_args(settings: Settings) -> dict[str, Any]:
    args: dict[str, Any] = {
        "server_settings": {
            "application_name": "ledgerlens-api",
            "statement_timeout": str(settings.db_statement_timeout_ms),
        },
        # A suspended serverless compute (Neon, Aurora Serverless) takes several
        # seconds to wake; measured cold connects to Neon run ~7 s. 10 s left no
        # margin. `wait_for_database` retries on top of this.
        "timeout": 20.0,
    }
    if settings.database_requires_tls:
        # Neon terminates TLS; asyncpg needs an SSLContext rather than `sslmode=`.
        args["ssl"] = ssl.create_default_context()
    return args


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build a fresh engine. Used by the app factory and by tests."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=280,  # below Neon's idle cutoff
        connect_args=_connect_args(settings),
        echo=False,
        future=True,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Initialise the process-wide engine and session factory (idempotent)."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
        logger.info("db_engine_initialised")
    return _engine


async def dispose_engine() -> None:
    """Close every pooled connection. Called on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("db_engine_disposed")
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Read-oriented session. Rolls back on exit; never commits implicitly."""
    async with get_session_factory()() as session:
        try:
            yield session
        finally:
            await session.rollback()


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """Write session wrapped in a single transaction: commit on success, rollback on error."""
    async with get_session_factory()() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a read session."""
    async with session_scope() as session:
        yield session
