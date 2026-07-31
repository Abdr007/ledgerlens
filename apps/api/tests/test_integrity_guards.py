"""Three guarantees that used to fail open, each quietly.

* A scan longer than the vision lane renders was extracted from its opening pages
  and could still be auto-committed, because a validator that reconciles part of a
  document reconciles it just as happily as the whole one.
* Redaction removed the secret and the surrounding JSON with it, so the log line
  that mattered most stopped being parseable at exactly the wrong moment.
* "The schema is current" was a claim about tables and indexes, while the rules it
  was standing in for are constraints.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.bootstrap import _EXPECTED_CONSTRAINTS, _schema_is_current
from app.core.logging import redact
from app.models.enums import Lane
from app.models.schemas import InvoiceExtraction, ValidationReport
from app.pipeline.extract import ExtractionOutcome

# --- F-7: a partially-read document is not a verified document -------------


def _outcome(*, pages_read: int | None, pages_total: int | None) -> ExtractionOutcome:
    return ExtractionOutcome(
        extraction=InvoiceExtraction(),
        report=ValidationReport(passed=True),
        raw_payload={},
        repair_attempts=0,
        model="claude-sonnet-5",
        lane=Lane.VISION,
        pages_read=pages_read,
        pages_total=pages_total,
    )


def test_a_fully_read_document_has_nothing_unread() -> None:
    assert _outcome(pages_read=3, pages_total=3).pages_unread == 0


def test_a_truncated_scan_reports_the_gap() -> None:
    assert _outcome(pages_read=3, pages_total=10).pages_unread == 7


def test_the_text_lane_has_no_page_gap() -> None:
    """PyMuPDF reads every page, so the counts are `None` and the gap is zero."""
    assert _outcome(pages_read=None, pages_total=None).pages_unread == 0


def test_a_corrupt_count_cannot_produce_a_negative_gap() -> None:
    assert _outcome(pages_read=5, pages_total=3).pages_unread == 0


# --- F-8: redaction keeps the record readable ------------------------------


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ('{"m":"x","api_key":"sk-ant-abcdefghijklmnop","path":"/v1"}', "sk-ant-abcdefghijklmnop"),
        ('{"m":"x","token":"abcdefghijklmnop","level":"INFO"}', "abcdefghijklmnop"),
        ('{"m":"x","password":"hunter2hunter2","level":"INFO"}', "hunter2hunter2"),
        ('{"h":"Authorization: Bearer abcdefghijklmnop"}', "abcdefghijklmnop"),
        ('{"db":"postgresql+asyncpg://user:swordfish@host/db"}', "swordfish"),
        ('{"m":"leaked sk-ant-abcdefghijklmnop inline"}', "sk-ant-abcdefghijklmnop"),
    ],
)
def test_a_redacted_line_is_still_json_and_still_scrubbed(line: str, secret: str) -> None:
    redacted = redact(line)
    assert secret not in redacted, "the secret survived redaction"
    # The whole point: a security-relevant line must not be the one your log
    # aggregator drops. Substituting the match wholesale ate the key name and the
    # value's opening quote, and the object stopped parsing.
    json.loads(redacted)


def test_the_label_survives_so_the_line_stays_diagnosable() -> None:
    redacted = redact('{"api_key":"sk-ant-abcdefghijklmnop"}')
    assert json.loads(redacted) == {"api_key": "[REDACTED]"}


def test_redaction_leaves_ordinary_lines_untouched() -> None:
    line = '{"message":"document_processed","latency_ms":94,"status":"DONE"}'
    assert redact(line) == line


# --- F-9: the schema probe covers the constraints, not just the tables -----


def test_the_probe_expects_every_named_check_and_unique_constraint() -> None:
    # The enum CHECKs and the anomaly de-duplication key. Named in `tables.py`,
    # so if one is renamed there and not here the assertion below fails loudly.
    assert "ck_documents_status" in _EXPECTED_CONSTRAINTS
    assert "ck_anomalies_severity" in _EXPECTED_CONSTRAINTS
    assert "uq_anomalies_document_fingerprint" in _EXPECTED_CONSTRAINTS
    assert len(_EXPECTED_CONSTRAINTS) >= 8


@pytest.mark.integration
async def test_a_missing_constraint_means_the_schema_is_not_current(engine: AsyncEngine) -> None:
    """Drop one CHECK and the probe must stop calling the schema current.

    Previously it looked only at tables, indexes and the audit trigger, so a
    database missing every enum CHECK passed — and in production, where
    `DB_MANAGE_SCHEMA` is false, nothing would ever have rebuilt them.
    """
    async with engine.connect() as connection:
        assert await _schema_is_current(connection) is True

    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE anomalies DROP CONSTRAINT ck_anomalies_severity")
        )
    try:
        async with engine.connect() as connection:
            assert await _schema_is_current(connection) is False
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE anomalies ADD CONSTRAINT ck_anomalies_severity "
                    "CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH'))"
                )
            )
        # This is the only test that runs DDL against the shared pool. asyncpg
        # caches prepared statements per connection, and altering the table leaves
        # every *other* pooled connection holding plans for a schema that no longer
        # matches. They are discarded on next use rather than closed, and the
        # orphaned sockets surface at session teardown as ResourceWarnings — which
        # this suite promotes to failures. Disposing returns the pool to a known
        # state; the engine reopens lazily for whatever runs next.
        await engine.dispose()
