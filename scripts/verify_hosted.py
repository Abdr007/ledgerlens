#!/usr/bin/env python3
"""Pre-flight the deployed stack, the way `make audit` pre-flights the source.

`make audit` proves 137 tests against a local PostgreSQL. Nothing proved anything
against the deployment, so every hosted failure so far was found by opening the
page and looking. Two of the worst leave no trace in the API's logs at all,
because in both cases no request ever reaches the API: a UI redirecting every
visitor to a login page, and a `connect-src` baked at build time that still names
the previous API host. Both look exactly like an outage from the outside.

Run it before a demo. It also warms the Space, which sleeps after 48 hours idle,
and the Neon compute behind it, which suspends sooner — so the first document of
the call is not the slow one.

    make verify-hosted

Standard library only, deliberately: this checks a deployment, and should not
itself depend on a working install.

## On cleaning up after itself

It does not, and cannot. `audit_log` is append-only, enforced by a database
trigger that raises on `UPDATE` and `DELETE` — including the cascade from
`documents`. That is the product's central claim, not an oversight, so a hosted
check is not given a back door through it.

What replaces deletion is idempotency. The document uploaded here is pinned: the
same bytes every run, so `UNIQUE(file_hash)` means the first run creates one row
and every run afterwards is told `duplicate` and creates nothing. The ledger
gains one permanent, clearly-named record and never grows again — and the second
run onwards turns the clean-up step into a live proof of the idempotency
guarantee, which is a better check than the one it replaces.

`--fresh` makes the bytes unique so the whole pipeline genuinely re-runs. That
adds a permanent row each time, which is why it is not the default.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

UI_DEFAULT: Final = "https://ledgerlens-jet.vercel.app"
API_DEFAULT: Final = "https://Abdr007-ledgerlens.hf.space"

#: A real degraded scan from the labelled evaluation set — a phone photo of an
#: invoice, committed to the repository, so this runs from a fresh clone.
SAMPLE: Final = REPO_ROOT / "eval" / "testset" / "EV-CE-002-scan.jpg"
SAMPLE_NAME: Final = "hosted-verification.jpg"

# A cold Space wakes in ~30 s, and the Neon compute behind it can take another
# few seconds to resume its first connection.
TIMEOUT_S: Final = 120
#: How long to wait for one document to reach a terminal state.
PIPELINE_DEADLINE_S: Final = 90

HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202
HTTP_FOUND: Final = 302
HTTP_TOO_MANY_REQUESTS: Final = 429

HIGH_SEVERITIES: Final = frozenset({"HIGH", "CRITICAL"})
#: Spec §2: Ingest -> Route -> Extract -> Validate -> Screen -> Ledger.
PIPELINE_STAGES: Final = ("ingest", "route", "extract", "validate", "screen", "ledger")


class CheckError(Exception):
    """A check that did not hold. The message is what the presenter needs to read."""


def _lower_keys(headers: Any) -> dict[str, str]:
    """Header names, lower-cased.

    ``dict(response.headers)`` keeps the wire casing and loses the case-insensitive
    lookup the underlying ``Message`` provides. Over HTTP/2 the names arrive
    lower-cased anyway, so a lower-cased lookup against that dict works — and then
    silently stops working over HTTP/1.1, where Vercel sends
    ``Content-Security-Policy``.
    """
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return int(response.status), _lower_keys(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), _lower_keys(exc.headers), exc.read()


def _json(url: str, *, origin: str, expect: int = HTTP_OK) -> Any:
    status, _, raw = _request(url, headers={"Origin": origin})
    if status != expect:
        raise CheckError(f"{url} returned HTTP {status}, expected {expect}")
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# Checks                                                                       #
# --------------------------------------------------------------------------- #


def check_ui_is_public(ui: str) -> str:
    """A 302 here means deployment protection is on and visitors hit a login page."""
    status, headers, _ = _request(ui)
    if status == HTTP_FOUND:
        location = headers.get("location", "")
        if "vercel.com" in location:
            raise CheckError(
                "the UI redirects to Vercel login — deployment protection is enabled. "
                "Project -> Settings -> Deployment Protection -> disable Vercel Authentication"
            )
        raise CheckError(f"the UI redirects to {location}")
    if status != HTTP_OK:
        raise CheckError(f"the UI returned HTTP {status}")
    return f"HTTP 200 at {ui}"


def check_csp_points_at_the_api(ui: str, api: str) -> str:
    """The CSP is baked at build time; a stale one blocks every call inside the browser."""
    _, headers, _ = _request(ui)
    csp = headers.get("content-security-policy", "")
    if not csp:
        raise CheckError("the UI sends no Content-Security-Policy")
    connect = next((part for part in csp.split(";") if part.strip().startswith("connect-src")), "")
    host = api.split("://", 1)[-1]
    if host.lower() not in connect.lower():
        raise CheckError(
            f"CSP connect-src does not name {host} — it reads '{connect.strip()}'. "
            "NEXT_PUBLIC_API_BASE_URL was not set at BUILD time; redeploy after setting it"
        )
    return f"connect-src names {host}"


def check_api_health(api: str, origin: str) -> str:
    data = _json(f"{api}/health", origin=origin)
    if data.get("status") != "ok":
        raise CheckError(
            f"health is '{data.get('status')}' with database '{data.get('database')}' — "
            "the API is up but its database is not; check DATABASE_URL on the Space"
        )
    if data.get("database") != "up":
        raise CheckError(f"database is '{data.get('database')}'")
    return (
        f"v{data.get('version')} · {data.get('environment')} · db up · "
        f"llm {data.get('llm_mode')} · langfuse {data.get('langfuse')}"
    )


def check_cors(api: str, ui: str) -> str:
    """The UI's origin is allowed; an unknown origin is not.

    A wildcard would pass the first half and fail the product: this API accepts
    uploads and mutates state, so the second half is the check that matters.
    """
    status, headers, _ = _request(
        f"{api}/v1/documents",
        method="OPTIONS",
        headers={
            "Origin": ui,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed = headers.get("access-control-allow-origin", "")
    if status != HTTP_OK or ui not in allowed:
        raise CheckError(
            f"pre-flight from {ui} returned HTTP {status} allow-origin='{allowed}'. "
            "Set ALLOWED_ORIGINS on the Space to the UI origin"
        )

    _, headers, _ = _request(
        f"{api}/v1/documents",
        method="OPTIONS",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    if headers.get("access-control-allow-origin"):
        raise CheckError(
            "an unknown origin was allowed — ALLOWED_ORIGINS is a wildcard or "
            "contains something it should not"
        )
    return f"{ui} allowed, evil.example refused"


def check_ledger_reconciles(api: str, origin: str) -> str:
    """The KPI cards and the ledger must be counting the same thing.

    Both read the same tables through different queries. They drifting apart is
    the quiet failure: a dashboard that shows a number nothing else agrees with.
    """
    stats = _json(f"{api}/v1/stats", origin=origin)
    listing = _json(f"{api}/v1/documents?limit=200", origin=origin)

    if listing["total"] != stats["documents_total"]:
        raise CheckError(
            f"stats says {stats['documents_total']} documents, the ledger says {listing['total']}"
        )
    counted: dict[str, int] = {}
    for item in listing["items"]:
        counted[item["status"]] = counted.get(item["status"], 0) + 1
    for label, key in (
        ("DONE", "documents_processed"),
        ("NEEDS_REVIEW", "documents_needs_review"),
        ("FAILED", "documents_failed"),
    ):
        if counted.get(label, 0) != stats[key]:
            raise CheckError(
                f"stats.{key}={stats[key]} but the ledger holds "
                f"{counted.get(label, 0)} {label} documents"
            )
    return (
        f"{stats['documents_total']} documents · {stats['documents_processed']} done · "
        f"{stats['documents_needs_review']} in review · {stats['anomalies_open']} open flags · "
        f"avg {stats['avg_latency_ms']:.0f} ms · p95 {stats['p95_latency_ms']:.0f} ms"
    )


def _routing_is_consistent(detail: dict[str, Any]) -> str | None:
    """The thesis, as an assertion: nothing failing verification sits in the ledger.

    Returns a description of the violation, or None when the record is consistent.
    """
    extraction = detail.get("extraction") or {}
    failed = [c["rule"] for c in extraction.get("checks", []) if not c["passed"]]
    high = [a for a in detail.get("anomalies", []) if a["severity"] in HIGH_SEVERITIES]
    status = detail["status"]

    if status == "DONE" and (failed or high):
        return (
            f"{detail['filename']} is DONE but has "
            f"{len(failed)} failed check(s) {failed} and {len(high)} high-severity flag(s)"
        )
    if status == "NEEDS_REVIEW" and not failed and not high:
        return f"{detail['filename']} is in review with nothing failing — no reason to be there"
    return None


def check_review_routing(api: str, origin: str) -> str:
    """Sweep every document in the live ledger against the product's own claim."""
    listing = _json(f"{api}/v1/documents?limit=200", origin=origin)
    violations: list[str] = []
    reviewed = 0
    for item in listing["items"]:
        detail = _json(f"{api}/v1/documents/{item['id']}", origin=origin)
        if detail["status"] not in {"DONE", "NEEDS_REVIEW"}:
            continue
        reviewed += 1
        if problem := _routing_is_consistent(detail):
            violations.append(problem)

    if violations:
        raise CheckError(f"{len(violations)} document(s) misrouted: {violations[0]}")
    return f"{reviewed} documents, every one routed by its own verification result"


def check_review_queue_is_explainable(api: str, origin: str) -> str:
    """Every flag carries a severity, a score and a reason a human can act on."""
    anomalies = _json(f"{api}/v1/anomalies?limit=100", origin=origin)
    if not anomalies:
        raise CheckError("the review queue is empty — nothing to demonstrate")
    for anomaly in anomalies:
        if not anomaly.get("reason"):
            raise CheckError(f"anomaly {anomaly['id']} carries no reason")
        if not anomaly.get("evidence"):
            raise CheckError(f"anomaly {anomaly['id']} carries no evidence")
        if anomaly.get("severity") not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise CheckError(f"anomaly {anomaly['id']} has severity '{anomaly.get('severity')}'")
    kinds = sorted({a["anomaly_type"] for a in anomalies})
    return f"{len(anomalies)} flag(s), all with severity + evidence + reason · {kinds}"


def _multipart(name: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----ledgerlensverify0000"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _await_terminal(api: str, origin: str, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + PIPELINE_DEADLINE_S
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _json(f"{api}/v1/documents/{document_id}/status", origin=origin)
        if state.get("is_terminal"):
            return state
        time.sleep(1)
    raise CheckError(
        f"the document was still '{state.get('status')}' after {PIPELINE_DEADLINE_S}s "
        f"(progress {state.get('progress')})"
    )


def check_round_trip(api: str, origin: str, *, fresh: bool) -> str:
    """Upload a real document and verify what came back out.

    On the first run this drives the whole pipeline. Afterwards the same bytes
    are recognised and no work is repeated — which is the guarantee, so it is
    asserted rather than worked around. See the module docstring.
    """
    if not SAMPLE.exists():
        raise CheckError(f"{SAMPLE} is missing from the checkout")

    payload = SAMPLE.read_bytes()
    name = SAMPLE_NAME
    if fresh:
        # Trailing bytes after the JPEG end-of-image marker change the SHA-256
        # without changing what any decoder reads.
        marker = str(time.time_ns()).encode()
        payload = payload + b"\xff\xfe" + len(marker).to_bytes(2, "big") + marker
        name = f"hosted-verification-{time.time_ns()}.jpg"

    body, content_type = _multipart(name, payload)
    status, _, raw = _request(
        f"{api}/v1/documents",
        method="POST",
        body=body,
        headers={"Content-Type": content_type, "Origin": origin},
    )
    if status != HTTP_ACCEPTED:
        raise CheckError(f"upload returned HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
    upload = json.loads(raw)

    state = _await_terminal(api, origin, upload["document_id"])
    detail = _json(f"{api}/v1/documents/{upload['document_id']}", origin=origin)

    if problem := _routing_is_consistent(detail):
        raise CheckError(problem)

    seen = {entry["stage"] for entry in detail.get("audit", []) if entry.get("stage")}
    missing = [stage for stage in PIPELINE_STAGES if stage not in seen]
    if missing:
        raise CheckError(f"the audit trail is missing stage(s) {missing} — it should span all six")

    extraction = detail.get("extraction") or {}
    checks = extraction.get("checks", [])
    if not checks:
        raise CheckError("no validation report was stored — nothing verified this document")

    passed = sum(1 for c in checks if c["passed"])
    origin_word = "deduplicated" if upload["duplicate"] else "processed"
    return (
        f"{origin_word} · {detail['status']} · lane {detail.get('lane')} · "
        f"{passed}/{len(checks)} checks passed · {len(seen)}/6 stages audited · "
        f"{state.get('latency_ms')} ms"
    )


def check_idempotency(api: str, origin: str) -> str:
    """The same bytes a second time must not create a second record."""
    before = _json(f"{api}/v1/stats", origin=origin)["documents_total"]

    body, content_type = _multipart(SAMPLE_NAME, SAMPLE.read_bytes())
    status, _, raw = _request(
        f"{api}/v1/documents",
        method="POST",
        body=body,
        headers={"Content-Type": content_type, "Origin": origin},
    )
    if status != HTTP_ACCEPTED:
        raise CheckError(f"re-upload returned HTTP {status}")
    upload = json.loads(raw)
    if not upload["duplicate"]:
        raise CheckError("identical bytes were not recognised as a duplicate")

    after = _json(f"{api}/v1/stats", origin=origin)["documents_total"]
    if after != before:
        raise CheckError(f"the ledger grew from {before} to {after} on a duplicate upload")
    return f"same SHA-256 -> duplicate, ledger still {after} documents"


def check_errors_are_typed(api: str, origin: str) -> str:
    """A rejected upload returns the error envelope, never a stack trace."""
    body, content_type = _multipart("payload.exe", b"MZ\x90\x00 this is not a document")
    status, _, raw = _request(
        f"{api}/v1/documents",
        method="POST",
        body=body,
        headers={"Content-Type": content_type, "Origin": origin},
    )
    text = raw.decode("utf-8", "replace")
    if "Traceback" in text or 'File "' in text:
        raise CheckError("a rejected upload leaked a stack trace")
    if status == HTTP_ACCEPTED:
        raise CheckError("an executable was accepted as a document")
    try:
        code = json.loads(text)["error"]["code"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CheckError(f"HTTP {status} was not the error envelope: {text[:160]}") from exc
    return f"HTTP {status} {code}, no stack trace"


def check_rate_limit_binds(api: str, origin: str) -> str:
    """The ingestion limit must actually stop someone, not merely be configured.

    Production reported `X-RateLimit-Limit: 10` on every upload and never returned
    429, because `TRUSTED_PROXY_COUNT` was one less than the host's real proxy
    depth: the limiter keyed on the address of whichever edge node answered, so
    callers were spread across a handful of buckets and none of them filled. A
    check that read the headers would have called that a pass.

    Rejected uploads are used deliberately. The limiter decorator runs before the
    handler, so a 415 consumes budget while writing nothing — the limit can be
    driven to its ceiling without adding a single row to an append-only ledger.
    """
    boundary = "----ledgerlensratelimit"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="rate-limit-probe.exe"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"MZ\x90\x00 deliberately not a document"
        + f"\r\n--{boundary}--\r\n".encode()
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": origin}

    limit: str | None = None
    attempts = 0
    # One more than the ceiling this project configures, with headroom for a
    # budget already partly spent by the round-trip checks above.
    for attempts in range(1, 26):
        status, response_headers, _ = _request(
            f"{api}/v1/documents", method="POST", body=body, headers=headers
        )
        limit = response_headers.get("x-ratelimit-limit", limit)
        if status == HTTP_TOO_MANY_REQUESTS:
            return f"429 after {attempts} rejected uploads (limit {limit}), 0 ledger writes"
        if status == HTTP_ACCEPTED:
            raise CheckError("an executable was accepted as a document")

    raise CheckError(
        f"{attempts} uploads in seconds and none was refused, while the API reported "
        f"X-RateLimit-Limit: {limit}. The limit is counted but does not bind — "
        "TRUSTED_PROXY_COUNT is almost certainly lower than the host's real proxy "
        "depth, so the key is a proxy address rather than the caller. The API logs a "
        "`proxy_depth_mismatch` warning naming the value to set."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", default=UI_DEFAULT)
    parser.add_argument("--api", default=API_DEFAULT)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="force a full pipeline run with unique bytes (adds a permanent ledger row)",
    )
    args = parser.parse_args(argv)
    ui, api = args.ui.rstrip("/"), args.api.rstrip("/")

    checks: list[tuple[str, Any]] = [
        ("UI is public", lambda: check_ui_is_public(ui)),
        ("CSP points at the API", lambda: check_csp_points_at_the_api(ui, api)),
        ("API health", lambda: check_api_health(api, ui)),
        ("CORS is locked", lambda: check_cors(api, ui)),
        ("ledger reconciles", lambda: check_ledger_reconciles(api, ui)),
        ("nothing invalid committed", lambda: check_review_routing(api, ui)),
        ("review queue explains itself", lambda: check_review_queue_is_explainable(api, ui)),
        ("upload · extract · validate", lambda: check_round_trip(api, ui, fresh=args.fresh)),
        ("re-upload is idempotent", lambda: check_idempotency(api, ui)),
        ("rejections are typed", lambda: check_errors_are_typed(api, ui)),
        ("rate limit binds", lambda: check_rate_limit_binds(api, ui)),
    ]

    print(f"\n  UI   {ui}\n  API  {api}\n")
    failures: list[str] = []
    for name, run in checks:
        started = time.perf_counter()
        try:
            detail = run()
            print(f"  PASS  {name:30} {detail}  [{time.perf_counter() - started:.1f}s]")
        except CheckError as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name:30} {exc}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name:30} unreachable: {exc}")

    if failures:
        print(f"\n  {len(failures)} of {len(checks)} checks failed — do not present\n")
        return 1
    print(f"\n  all {len(checks)} checks passed · the Space is warm · ready to present\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
