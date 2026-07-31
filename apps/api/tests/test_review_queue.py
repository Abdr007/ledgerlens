"""The review queue: what reaches the front of it, and who gets to clear a flag.

Both regressions here only appear once the queue has more than a page of work or
more than one reviewer — which is to say, only in the conditions the queue exists
for. Severity was applied after `LIMIT`, so "most severe first" stopped being true
past page one; and a resolve that lost a race still wrote itself into the
append-only log as though it had won.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.db import transaction
from app.main import create_app
from app.models.enums import (
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    AuditEvent,
    DocumentStatus,
)
from app.models.tables import Anomaly, AuditLog, Document

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(clean_db: None) -> AsyncIterator[httpx.AsyncClient]:
    """The HTTP surface, without running the application lifespan.

    Deliberately not wrapped in `LifespanManager`. Shutdown calls
    `dispose_engine()`, which disposes the *process-wide* engine that the
    session-scoped `engine` fixture also owns — survivable for one module, but a
    second module doing it leaves the pool disposed underneath tests that are
    still using it, and the suite's zero-warning policy turns the resulting
    unclosed socket into a failure. Nothing these tests touch needs the lifespan:
    the schema comes from the fixtures, and `app.state.limiter` is wired in
    `create_app`.
    """
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ledgerlens.test",
        timeout=30.0,
    ) as client:
        yield client


async def _document(suffix: str) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with transaction() as session:
        session.add(
            Document(
                id=document_id,
                file_hash=suffix.rjust(64, "0"),
                filename=f"{suffix}.pdf",
                media_type="application/pdf",
                size_bytes=1024,
                status=str(DocumentStatus.DONE),
            )
        )
    return document_id


async def _anomaly(
    document_id: uuid.UUID, severity: AnomalySeverity, *, age_days: int, fingerprint: str
) -> uuid.UUID:
    anomaly_id = uuid.uuid4()
    async with transaction() as session:
        session.add(
            Anomaly(
                id=anomaly_id,
                document_id=document_id,
                anomaly_type=str(AnomalyType.ROUND_NUMBER),
                severity=str(severity),
                reason=f"{severity} finding",
                evidence={},
                fingerprint=fingerprint,
                status=str(AnomalyStatus.OPEN),
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
    return anomaly_id


# --- F-5: severity ordering survives pagination ----------------------------


async def test_the_most_severe_flag_leads_the_queue_even_when_it_is_the_oldest(
    api: httpx.AsyncClient,
) -> None:
    """The failure the Python sort hid: order by date, page, *then* sort.

    With the HIGH finding older than every LOW one, a page-sized LIMIT taken in
    date order contains no HIGH row at all — so re-sorting the page could never
    surface it, and the queue's own description became false at page one.
    """
    document_id = await _document("a1")
    await _anomaly(document_id, AnomalySeverity.HIGH, age_days=90, fingerprint="old-high")
    for day in range(4):
        await _anomaly(
            document_id, AnomalySeverity.LOW, age_days=day, fingerprint=f"recent-low-{day}"
        )

    first_page = (await api.get("/v1/anomalies", params={"limit": 1})).json()
    assert [item["severity"] for item in first_page] == [str(AnomalySeverity.HIGH)]


async def test_the_whole_queue_is_ordered_by_severity_then_recency(
    api: httpx.AsyncClient,
) -> None:
    document_id = await _document("a2")
    await _anomaly(document_id, AnomalySeverity.LOW, age_days=1, fingerprint="low")
    await _anomaly(document_id, AnomalySeverity.HIGH, age_days=2, fingerprint="high")
    await _anomaly(document_id, AnomalySeverity.MEDIUM, age_days=3, fingerprint="medium")

    severities = [item["severity"] for item in (await api.get("/v1/anomalies")).json()]
    assert severities == [
        str(AnomalySeverity.HIGH),
        str(AnomalySeverity.MEDIUM),
        str(AnomalySeverity.LOW),
    ]


async def test_paging_never_repeats_or_skips_a_flag(api: httpx.AsyncClient) -> None:
    """A stable total order is what makes LIMIT/OFFSET paging coherent at all."""
    document_id = await _document("a3")
    for index, severity in enumerate(
        [AnomalySeverity.LOW, AnomalySeverity.HIGH, AnomalySeverity.MEDIUM] * 2
    ):
        await _anomaly(document_id, severity, age_days=index, fingerprint=f"f{index}")

    seen: list[str] = []
    for offset in range(0, 6, 2):
        page = (await api.get("/v1/anomalies", params={"limit": 2, "offset": offset})).json()
        seen.extend(item["id"] for item in page)

    assert len(seen) == len(set(seen)) == 6


# --- F-6: a resolve that loses the race writes nothing ---------------------


async def test_only_one_of_two_simultaneous_reviewers_wins(api: httpx.AsyncClient) -> None:
    """Both read OPEN; Postgres lets one UPDATE through and the other matches zero.

    The loser must not append ANOMALY_RESOLVED naming its own decision — the
    trigger would make that permanent — and must not return 200 carrying the
    winner's outcome as though it were the caller's own.
    """
    document_id = await _document("a4")
    anomaly_id = await _anomaly(
        document_id, AnomalySeverity.HIGH, age_days=0, fingerprint="contested"
    )

    approve, reject = await asyncio.gather(
        api.post(f"/v1/anomalies/{anomaly_id}/resolve", json={"action": "approve"}),
        api.post(f"/v1/anomalies/{anomaly_id}/resolve", json={"action": "reject"}),
    )
    codes = sorted([approve.status_code, reject.status_code])
    assert codes == [200, 409], f"expected exactly one winner, got {codes}"

    loser = approve if approve.status_code == 409 else reject
    assert loser.json()["error"]["code"] == "invalid_state_transition"

    async with transaction() as session:
        resolutions = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.document_id == document_id,
                    AuditLog.event == str(AuditEvent.ANOMALY_RESOLVED),
                )
            )
        ).scalar_one()
    assert resolutions == 1, "the losing reviewer must not appear in the audit trail"


async def test_resolving_an_already_reviewed_flag_is_refused(api: httpx.AsyncClient) -> None:
    document_id = await _document("a5")
    anomaly_id = await _anomaly(
        document_id, AnomalySeverity.MEDIUM, age_days=0, fingerprint="settled"
    )

    assert (
        await api.post(f"/v1/anomalies/{anomaly_id}/resolve", json={"action": "approve"})
    ).status_code == 200
    second = await api.post(f"/v1/anomalies/{anomaly_id}/resolve", json={"action": "reject"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"
