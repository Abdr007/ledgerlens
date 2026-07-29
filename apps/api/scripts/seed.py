"""Seed the ledger with realistic vendor history (spec §8).

Loads 30 historical invoices across 6 vendors so the vendor-spend chart and the
per-vendor z-scores are meaningful the first time the dashboard is opened, and so
the planted near-duplicate pair guarantees the anomaly demo fires.

Documents are rendered as **real PDFs and real degraded scans** and pushed through
the **real pipeline** in the same process — the same hashing, routing, extraction,
validation, screening and persistence a browser upload takes. Nothing is inserted
straight into the tables.

Order matters: a duplicate can only be found against something already in the
ledger, so the corpus is processed oldest-first.

    python scripts/seed.py            # add to whatever is already there
    python scripts/seed.py --reset    # wipe the ledger first
    python scripts/seed.py --scans    # render a subset as photographed scans
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Allow `python scripts/seed.py` from the apps/api directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.bootstrap import init_schema
from app.core.db import dispose_engine, init_engine, transaction
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.core.tracing import get_tracer
from app.deps import get_claude_client
from app.devtools.corpus import build_seed_corpus
from app.devtools.documents import degrade_to_scan, render_invoice_pdf
from app.models.enums import DocumentStatus
from app.pipeline.orchestrator import PipelineOrchestrator

_TABLES = ("anomalies", "extractions", "llm_traces", "audit_log", "failed_jobs", "documents")


async def reset_ledger() -> None:
    """Empty every table. The audit-log trigger blocks DELETE, so use TRUNCATE."""
    async with transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))


async def seed(*, reset: bool, with_scans: bool) -> int:
    settings = get_settings()
    configure_logging("WARNING")  # the progress table below is the useful output

    engine = init_engine(settings)
    await init_schema(engine)
    if reset:
        await reset_ledger()
        print("Ledger reset.\n")

    client = get_claude_client()
    orchestrator = PipelineOrchestrator(client=client, settings=settings, tracer=get_tracer())

    corpus = build_seed_corpus()
    print(f"Seeding {len(corpus)} invoices across 6 vendors (mode: {client.mode})\n")
    print(f"{'#':>3}  {'invoice':16} {'vendor':30} {'total':>12}  {'status':13} {'ms':>5}  flags")
    print("-" * 96)

    failures = 0
    started = time.perf_counter()

    for index, item in enumerate(corpus, start=1):
        pdf = render_invoice_pdf(item.spec)
        # A slice of the corpus is photographed so the vision lane has real input.
        as_scan = with_scans and index % 7 == 0
        payload = degrade_to_scan(pdf) if as_scan else pdf
        filename = f"{item.stem}{'.jpg' if as_scan else '.pdf'}"
        content_type = "image/jpeg" if as_scan else "application/pdf"

        outcome = await orchestrator.ingest(
            data=payload, filename=filename, declared_content_type=content_type
        )
        if outcome.duplicate:
            print(f"{index:>3}  {item.spec.invoice_number:16} {'(already ingested)':30}")
            continue

        await orchestrator.process(
            document_id=outcome.document_id,
            data=payload,
            filename=filename,
            media_type=outcome.media_type,
        )

        async with transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT d.status, d.latency_ms, "
                        "  (SELECT count(*) FROM anomalies a WHERE a.document_id = d.id) AS flags "
                        "FROM documents d WHERE d.id = :id"
                    ),
                    {"id": outcome.document_id},
                )
            ).one()

        status = DocumentStatus(row.status)
        if status is DocumentStatus.FAILED:
            failures += 1
        marker = "  <-- ANOMALY" if row.flags else ""
        print(
            f"{index:>3}  {item.spec.invoice_number:16} {item.spec.vendor:30} "
            f"{item.spec.total:>12,.2f}  {status.value:13} {row.latency_ms or 0:>5}  "
            f"{row.flags}{marker}"
        )

    elapsed = time.perf_counter() - started

    async with transaction() as session:
        summary = (
            await session.execute(
                text(
                    "SELECT "
                    "  (SELECT count(*) FROM documents) AS docs, "
                    "  (SELECT count(*) FROM documents WHERE status='DONE') AS done, "
                    "  (SELECT count(*) FROM documents WHERE status='NEEDS_REVIEW') AS review, "
                    "  (SELECT count(*) FROM documents WHERE status='FAILED') AS failed, "
                    "  (SELECT count(*) FROM anomalies) AS anomalies, "
                    "  (SELECT count(*) FROM audit_log) AS audit, "
                    "  (SELECT coalesce(sum(cost_usd),0) FROM documents) AS cost, "
                    "  (SELECT coalesce(avg(latency_ms),0) FROM documents "
                    "     WHERE latency_ms IS NOT NULL) AS avg_ms"
                )
            )
        ).one()

    print("-" * 96)
    print(
        f"\n{summary.docs} documents · {summary.done} DONE · {summary.review} NEEDS_REVIEW · "
        f"{summary.failed} FAILED"
    )
    print(
        f"{summary.anomalies} anomaly flag(s) · {summary.audit} audit events · "
        f"avg {float(summary.avg_ms):.0f} ms/doc · ${float(summary.cost):.4f} total"
    )
    print(f"Seeded in {elapsed:.1f}s.\n")

    await dispose_engine()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed LedgerLens with vendor history.")
    parser.add_argument("--reset", action="store_true", help="Empty the ledger first.")
    parser.add_argument(
        "--scans",
        action="store_true",
        help="Render part of the corpus as photographed scans (needs a Claude key to read).",
    )
    args = parser.parse_args()
    return asyncio.run(seed(reset=args.reset, with_scans=args.scans))


if __name__ == "__main__":
    raise SystemExit(main())
