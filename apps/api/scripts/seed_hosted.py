"""Seed the *deployed* ledger by uploading through the API, not around it.

`seed.py` runs the pipeline in-process against whatever `DATABASE_URL` points at.
That is right for a local stack and wrong for a hosted one, for a reason AUDIT.md
§4b measured: a document costs roughly 42 database round trips, so seeding a Neon
database from a laptop records **~8.9 s per document** where the same work costs
milliseconds in-region. Those numbers then land on the dashboard's latency KPI,
which would be reporting the seeder's distance from Virginia rather than anything
about the pipeline.

So this renders the corpus locally and uploads it to the deployed API, which
processes each document beside its database. The recorded latency is then the
pipeline's, which is the only version of that number worth showing.

    python scripts/seed_hosted.py --api https://Abdr007-ledgerlens.hf.space --reset

Order matters, exactly as it does locally: a duplicate can only be found against
something already in the ledger, so the corpus is uploaded oldest-first and each
document is driven to a terminal state before the next one starts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Allow `python scripts/seed_hosted.py` from the apps/api directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.db import dispose_engine, init_engine, transaction
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.devtools.corpus import build_seed_corpus
from app.devtools.documents import render_invoice_pdf

_TABLES = ("anomalies", "extractions", "llm_traces", "audit_log", "failed_jobs", "documents")

#: Ingestion is limited to 10 requests per minute per IP. Pacing just inside that
#: is simpler and safer than raising the production limit for convenience, and it
#: keeps the seed honest: it goes through the same door everyone else does.
_PACE_S = 6.3
_TERMINAL_DEADLINE_S = 120
_HTTP_OK = 200
_HTTP_ACCEPTED = 202
_HTTP_TOO_MANY = 429


def _request(
    url: str, *, method: str = "GET", body: bytes | None = None, ctype: str | None = None
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
    if ctype:
        request.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            parsed: dict[str, Any] = json.loads(response.read() or b"{}")
            return int(response.status), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            failed: dict[str, Any] = json.loads(raw or b"{}")
            return int(exc.code), failed
        except json.JSONDecodeError:
            return int(exc.code), {"raw": raw[:200].decode("utf-8", "replace")}


def _upload(api: str, name: str, payload: bytes, ctype: str) -> dict[str, Any]:
    boundary = "----ledgerlensseed"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    for _ in range(6):
        status, data = _request(
            f"{api}/v1/documents",
            method="POST",
            body=body,
            ctype=f"multipart/form-data; boundary={boundary}",
        )
        if status == _HTTP_TOO_MANY:
            time.sleep(_PACE_S)
            continue
        if status != _HTTP_ACCEPTED:
            msg = f"upload of {name} returned HTTP {status}: {data}"
            raise RuntimeError(msg)
        return data
    msg = f"upload of {name} was rate-limited repeatedly"
    raise RuntimeError(msg)


def _await_terminal(api: str, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _TERMINAL_DEADLINE_S
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, state = _request(f"{api}/v1/documents/{document_id}/status")
        if state.get("is_terminal"):
            return state
        time.sleep(1)
    msg = f"{document_id} never reached a terminal state"
    raise RuntimeError(msg)


async def reset_hosted_ledger() -> None:
    """Empty every table. The audit-log trigger blocks DELETE, so use TRUNCATE.

    TRUNCATE is not a way around the append-only guarantee — the guarantee is that
    individual history cannot be rewritten, and this empties the environment
    wholesale, visibly, through a path the local `make reset` already uses. It is
    the difference between resetting a demo and editing a record.
    """
    init_engine(get_settings())
    try:
        async with transaction() as session:
            await session.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="base URL of the deployed API")
    parser.add_argument("--reset", action="store_true", help="empty the hosted ledger first")
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")

    configure_logging("WARNING")

    status, health = _request(f"{api}/health")
    if status != _HTTP_OK or health.get("database") != "up":
        print(f"  {api} is not healthy: HTTP {status} {health}")
        return 1
    print(f"  target  {api}  ({health.get('environment')}, llm {health.get('llm_mode')})")

    if args.reset:
        asyncio.run(reset_hosted_ledger())
        print("  ledger reset\n")

    corpus = build_seed_corpus()
    print(f"  uploading {len(corpus)} invoices through the API, paced under the rate limit\n")
    print(f"{'#':>3}  {'invoice':16} {'vendor':30} {'total':>12}  {'status':13} {'ms':>5}  flags")
    print("-" * 96)

    started = time.perf_counter()
    flagged = 0

    for index, item in enumerate(corpus, start=1):
        pdf = render_invoice_pdf(item.spec)
        upload = _upload(api, f"{item.stem}.pdf", pdf, "application/pdf")
        if upload["duplicate"]:
            print(f"{index:>3}  {item.spec.invoice_number:16} {'(already ingested)':30}")
            continue

        state = _await_terminal(api, upload["document_id"])
        latency = int(state.get("latency_ms") or 0)
        flags = int(state.get("anomaly_count") or 0)
        flagged += 1 if flags else 0
        marker = "  <-- ANOMALY" if flags else ""
        print(
            f"{index:>3}  {item.spec.invoice_number:16} {item.spec.vendor:30} "
            f"{item.spec.total:>12,.2f}  {state.get('status')!s:13} {latency:>5}  "
            f"{flags}{marker}"
        )
        if index < len(corpus):
            time.sleep(_PACE_S)

    _, stats = _request(f"{api}/v1/stats")
    print("-" * 96)
    print(
        f"\n  {stats['documents_total']} documents · {stats['documents_processed']} DONE · "
        f"{stats['documents_needs_review']} NEEDS_REVIEW · {stats['documents_failed']} FAILED · "
        f"{stats['anomalies_total']} anomalies"
    )
    print(
        f"  avg {stats['avg_latency_ms']:.0f} ms · p95 {stats['p95_latency_ms']:.0f} ms "
        f"(in-region, recorded by the API) · wall clock {time.perf_counter() - started:.0f}s"
    )
    if not flagged:
        print("\n  WARNING: no anomaly was raised. The planted duplicate should have fired.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
