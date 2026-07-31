"""Failure classification and the honesty of the ledger under a lost race.

Two regressions that only show up when something goes wrong: a stalled model call
was classified two different ways depending on which of two identical deadlines
fired first, and a status transition that lost a race still wrote itself into the
append-only audit log as though it had happened.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.claude import LiveClaudeClient
from app.core.db import transaction
from app.core.retry import RetriesExhaustedError, RetryPolicy, run_with_retry
from app.core.settings import Settings
from app.models.enums import AuditEvent, DocumentStatus
from app.models.tables import AuditLog, Document
from app.pipeline.orchestrator import PipelineOrchestrator

# --- F-2: a wait_for timeout is retryable, and the SDK trips first ----------


def test_outer_bound_sits_above_the_sdk_timeout_so_they_cannot_race() -> None:
    settings = Settings(anthropic_api_key="sk-ant-not-a-real-key", llm_mode="live")
    client = LiveClaudeClient(settings)
    assert client._policy.timeout_s > settings.llm_timeout_s
    assert TimeoutError in client._policy.retry_on


async def test_wait_for_timeout_is_retried() -> None:
    calls = 0

    async def stalls() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)

    policy = RetryPolicy(attempts=2, base_delay_s=0.0, timeout_s=0.01, retry_on=(TimeoutError,))
    with pytest.raises(RetriesExhaustedError):
        await run_with_retry("stalls", stalls, policy)

    assert calls == 2, "the outer timeout must consume the retry budget, not bypass it"


async def test_without_timeout_error_the_budget_is_bypassed() -> None:
    """Documents the defect: `wait_for` raises the built-in `TimeoutError`, which is
    unrelated to `anthropic.APITimeoutError`, so a policy listing only the SDK's
    error abandons the call on attempt one and reports it as an internal fault."""
    calls = 0

    async def stalls() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1.0)

    policy = RetryPolicy(attempts=3, base_delay_s=0.0, timeout_s=0.01, retry_on=(ConnectionError,))
    with pytest.raises(TimeoutError):
        await run_with_retry("stalls", stalls, policy)

    assert calls == 1


# --- F-3: a lost transition is not written into the audit log --------------


@pytest.mark.integration
async def test_only_the_winning_transition_is_audited(
    orchestrator: PipelineOrchestrator, clean_db: None
) -> None:
    """Two finalisers race one PROCESSING row; exactly one transition is real.

    Both read `PROCESSING`, both pass the transition check, and both attempt the
    conditional UPDATE. Postgres lets one through and the loser matches zero rows —
    at which point it must not claim a state change, because the append-only trigger
    makes anything it writes permanent.
    """
    document_id = uuid.uuid4()
    async with transaction() as session:
        session.add(
            Document(
                id=document_id,
                file_hash="f" * 64,
                filename="race.pdf",
                media_type="application/pdf",
                size_bytes=1024,
                status=str(DocumentStatus.PROCESSING),
            )
        )

    await asyncio.gather(
        orchestrator._finalise(
            document_id=document_id,
            target=DocumentStatus.DONE,
            reason=None,
            latency_ms=10,
            cost_usd=Decimal("0"),
        ),
        orchestrator._finalise(
            document_id=document_id,
            target=DocumentStatus.NEEDS_REVIEW,
            reason="review",
            latency_ms=10,
            cost_usd=Decimal("0"),
        ),
    )

    async with transaction() as session:
        transitions = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.document_id == document_id,
                    AuditLog.event == str(AuditEvent.STATUS_CHANGED),
                )
            )
        ).scalar_one()
        final = (
            await session.execute(select(Document.status).where(Document.id == document_id))
        ).scalar_one()

    assert transitions == 1, "the losing finaliser must not audit a transition it never made"
    assert final in {str(DocumentStatus.DONE), str(DocumentStatus.NEEDS_REVIEW)}
