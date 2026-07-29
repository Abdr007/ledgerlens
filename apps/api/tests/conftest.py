"""Test fixtures.

Integration tests run against a **real PostgreSQL database**, not SQLite and not
a mock. Every guarantee this project makes — `UNIQUE(file_hash)`,
`INSERT … ON CONFLICT`, the conditional status transition, the append-only
trigger — is a Postgres behaviour. Testing them anywhere else would prove
nothing about production.

Set `TEST_DATABASE_URL` to point at a throwaway database; the suite skips its
integration tests if none is reachable, so unit tests still run anywhere.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

# Configure the environment before any app module reads settings.
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://ledgerlens:ledgerlens@localhost:5433/ledgerlens_test",
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["LLM_MODE"] = "stub"  # never touch the network from a test
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000"
# Treat X-Forwarded-For as authoritative in tests so each case can present a
# distinct client IP and get its own rate-limit bucket.
os.environ["TRUSTED_PROXY_COUNT"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.bootstrap import init_schema
from app.core.claude import OfflineClaudeClient
from app.core.db import _connect_args, dispose_engine, init_engine, transaction
from app.core.settings import Settings, get_settings
from app.core.tracing import LocalTracer
from app.devtools.corpus import build_seed_corpus
from app.devtools.documents import InvoiceSpec, render_invoice_pdf
from app.pipeline.offline import build_offline_handlers
from app.pipeline.orchestrator import PipelineOrchestrator

_TABLES = ("anomalies", "extractions", "llm_traces", "audit_log", "failed_jobs", "documents")

# PostgreSQL: 3D000 invalid_catalog_name — the server answered, the database does not exist.
_UNDEFINED_DATABASE = "3D000"


def _is_missing_database(exc: BaseException) -> bool:
    """True when the server answered but the database does not exist.

    Walks the cause chain: asyncpg raises `InvalidCatalogNameError` at connect
    time, before SQLAlchemy wraps it, so the driver exception may be either the
    outermost error or a cause of one.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == _UNDEFINED_DATABASE:
            return True
        if _UNDEFINED_DATABASE in str(current) or "does not exist" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


async def _create_test_database(settings: Settings) -> None:
    """Create the test database, connecting through the server's maintenance DB.

    Without this, a fresh clone that runs `make up && make test` finds no
    `ledgerlens_test`, every integration fixture errors, and the suite *skips* —
    printing green while having exercised nothing. A missing test database is a
    setup step, not a reason to stop testing.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    url = make_url(settings.sqlalchemy_url)
    target = url.database
    admin_url = url.set(database="postgres")
    admin = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        connect_args=_connect_args(settings),
    )
    try:
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            )
            if not exists:
                # Identifier, so it cannot be bound as a parameter; the value comes
                # from our own configuration, never from a request.
                await connection.execute(text(f'CREATE DATABASE "{target}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """A live engine against the test database.

    Skips only when the PostgreSQL *server* cannot be reached — the one case a
    developer without Docker legitimately hits. Anything else fails loudly.
    """
    try:
        instance = init_engine(settings)
        await init_schema(instance)
    except Exception as exc:  # narrowed immediately: recoverable, or skip
        if not _is_missing_database(exc):
            pytest.skip(f"PostgreSQL is not reachable for integration tests: {exc}")
        await dispose_engine()
        await _create_test_database(settings)
        instance = init_engine(settings)
        await init_schema(instance)
    yield instance
    await dispose_engine()


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    """Empty every table before each test.

    TRUNCATE rather than DELETE: the audit log has a trigger that rejects DELETE,
    which is exactly the guarantee `test_audit_log_is_append_only` asserts.
    """
    async with transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def offline_client() -> OfflineClaudeClient:
    client = OfflineClaudeClient()
    for name, handler in build_offline_handlers().items():
        client.register(name, handler)
    return client


@pytest.fixture
def orchestrator(settings: Settings, offline_client: OfflineClaudeClient) -> PipelineOrchestrator:
    return PipelineOrchestrator(client=offline_client, settings=settings, tracer=LocalTracer())


@pytest.fixture(scope="session")
def invoice_specs() -> list[InvoiceSpec]:
    return [item.spec for item in build_seed_corpus()]


@pytest.fixture(scope="session")
def sample_pdf(invoice_specs: list[InvoiceSpec]) -> bytes:
    """A real, clean invoice PDF with a real text layer."""
    return render_invoice_pdf(invoice_specs[0])


@pytest.fixture(scope="session")
def sample_spec(invoice_specs: list[InvoiceSpec]) -> InvoiceSpec:
    return invoice_specs[0]


@pytest.fixture
def tmp_documents(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
