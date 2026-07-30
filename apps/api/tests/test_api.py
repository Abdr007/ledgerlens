"""HTTP surface: error envelopes, CORS lock, rate limits, security headers.

Driven through the real ASGI app so middleware, dependency wiring and exception
handlers are all exercised — not the route functions in isolation.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.devtools.documents import InvoiceSpec, render_invoice_pdf
from app.main import create_app

pytestmark = pytest.mark.integration

_IP_COUNTER = iter(range(10, 250))


def _fresh_ip() -> str:
    """A distinct client identity per test, so rate-limit buckets never bleed."""
    return f"198.51.100.{next(_IP_COUNTER)}"


# asgi_lifespan defaults to a 5 s start-up budget. That is ample against a local
# container, but a hosted database adds a real round trip to every start-up
# statement and a suspended serverless compute adds a cold start on top. The suite
# is meant to run against either, so the budget is generous — it exists to catch a
# hung boot, not to assert a latency figure.
_LIFESPAN_TIMEOUT_S = float(os.environ.get("TEST_LIFESPAN_TIMEOUT_S", "120"))


@pytest.fixture
async def api(clean_db: None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with LifespanManager(
        app, startup_timeout=_LIFESPAN_TIMEOUT_S, shutdown_timeout=_LIFESPAN_TIMEOUT_S
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://ledgerlens.test", timeout=30.0
        ) as client:
            yield client


#: What `conftest` sets `ALLOWED_ORIGINS` to. The origin guard is keyed on it.
ALLOWED_ORIGIN = "http://localhost:3000"


def _upload(
    pdf: bytes,
    name: str = "invoice.pdf",
    ip: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    headers = {"X-Forwarded-For": ip or _fresh_ip()}
    if origin:
        headers["Origin"] = origin
    return {"files": {"file": (name, pdf, "application/pdf")}, "headers": headers}


# ---------------------------------------------------------------------------
# Health & docs
# ---------------------------------------------------------------------------


async def test_health_reports_dependencies(api: httpx.AsyncClient) -> None:
    response = await api.get("/health", headers={"X-Forwarded-For": _fresh_ip()})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["llm_mode"] == "offline"


async def test_openapi_documents_the_whole_surface(api: httpx.AsyncClient) -> None:
    """Spec §3: auto OpenAPI docs at /docs — interviewers open this."""
    response = await api.get("/openapi.json", headers={"X-Forwarded-For": _fresh_ip()})
    assert response.status_code == 200
    paths = response.json()["paths"]
    for route in (
        "/v1/documents",
        "/v1/documents/{document_id}/status",
        "/v1/documents/{document_id}/audit",
        "/v1/anomalies",
        "/v1/anomalies/{anomaly_id}/resolve",
        "/v1/stats",
        "/health",
    ):
        assert route in paths, f"{route} missing from the OpenAPI document"


# ---------------------------------------------------------------------------
# Security headers & CORS
# ---------------------------------------------------------------------------


async def test_security_headers_are_present(api: httpx.AsyncClient) -> None:
    headers = (await api.get("/health", headers={"X-Forwarded-For": _fresh_ip()})).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["x-request-id"]


async def test_cors_allows_the_configured_origin(api: httpx.AsyncClient) -> None:
    response = await api.options(
        "/v1/documents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "X-Forwarded-For": _fresh_ip(),
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


async def test_cors_rejects_an_unknown_origin(api: httpx.AsyncClient) -> None:
    """Spec §7: CORS locked to the web origin — never a wildcard."""
    response = await api.options(
        "/v1/documents",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "X-Forwarded-For": _fresh_ip(),
        },
    )
    allowed = response.headers.get("access-control-allow-origin")
    assert allowed != "https://evil.example"
    assert allowed != "*"


# ---------------------------------------------------------------------------
# Upload contract
# ---------------------------------------------------------------------------


async def test_upload_accepts_a_pdf_and_reports_the_hash(
    api: httpx.AsyncClient, sample_pdf: bytes
) -> None:
    response = await api.post("/v1/documents", **_upload(sample_pdf))
    assert response.status_code == 202
    body = response.json()
    assert len(body["file_hash"]) == 64
    assert body["duplicate"] is False
    assert body["status"] == "PENDING"
    uuid.UUID(body["document_id"])  # must be a real UUID


async def test_reupload_is_reported_as_a_duplicate(
    api: httpx.AsyncClient, sample_pdf: bytes
) -> None:
    ip = _fresh_ip()
    first = (await api.post("/v1/documents", **_upload(sample_pdf, ip=ip))).json()
    second = (await api.post("/v1/documents", **_upload(sample_pdf, "other.pdf", ip=ip))).json()
    assert second["duplicate"] is True
    assert second["document_id"] == first["document_id"]


@pytest.mark.parametrize(
    ("payload", "content_type", "status", "code"),
    [
        (b"plain text file", "text/plain", 415, "unsupported_media_type"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "application/pdf", 415, "unsupported_media_type"),
        (b"", "application/pdf", 400, "empty_file"),
    ],
)
async def test_rejected_uploads_return_typed_errors(
    api: httpx.AsyncClient, payload: bytes, content_type: str, status: int, code: str
) -> None:
    response = await api.post(
        "/v1/documents",
        files={"file": ("x", payload, content_type)},
        headers={"X-Forwarded-For": _fresh_ip()},
    )
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    # Spec §7: typed error responses, never stack traces.
    assert "Traceback" not in response.text
    assert 'File "' not in response.text


async def test_oversized_upload_is_rejected(api: httpx.AsyncClient) -> None:
    payload = b"%PDF-" + b"\0" * (11 * 1024 * 1024)
    response = await api.post(
        "/v1/documents",
        files={"file": ("big.pdf", payload, "application/pdf")},
        headers={"X-Forwarded-For": _fresh_ip()},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


# ---------------------------------------------------------------------------
# Rate limiting (spec §7: 10 req/min/IP)
# ---------------------------------------------------------------------------


async def test_upload_rate_limit_is_enforced_per_ip(
    api: httpx.AsyncClient, invoice_specs: list[InvoiceSpec]
) -> None:
    # One PDF, sent twelve times. Deliberate: every request after the first takes
    # the deduplication path, so no pipeline runs and the twelve requests land well
    # inside the one-minute window. Sending twelve *different* PDFs makes the test
    # a throughput race instead of a rate-limit test — against a hosted database
    # each pipeline costs seconds, the window expires mid-loop and the limit never
    # appears to trigger. The limiter counts a deduplicated upload exactly like a
    # fresh one, which is the property that matters: an attacker must not be able
    # to spend the expensive path by replaying a file the server already has.
    ip = _fresh_ip()
    pdf = render_invoice_pdf(invoice_specs[0])
    codes: list[int] = []
    for index in range(12):
        response = await api.post("/v1/documents", **_upload(pdf, f"{index}.pdf", ip=ip))
        codes.append(response.status_code)

    assert 429 in codes, f"the 10/minute upload limit never triggered: {codes}"
    assert codes.count(202) <= 10

    limited = next(code for code in reversed(codes) if code == 429)
    assert limited == 429


async def test_a_cross_origin_write_is_refused_by_the_server(api: httpx.AsyncClient) -> None:
    """CORS headers ask a browser to enforce; this enforces.

    On Hugging Face Spaces the platform answers the pre-flight at its edge and
    echoes whatever `Origin` it was sent, so `ALLOWED_ORIGINS` never reaches the
    browser — the same image refuses `evil.example` locally and permits it through
    the Space (AUDIT.md §4c). A header something upstream can rewrite is not a
    control, so the write is refused here instead.
    """
    response = await api.post(
        "/v1/documents",
        headers={"Origin": "https://evil.example", "X-Forwarded-For": _fresh_ip()},
        files={"file": ("x.pdf", b"%PDF-1.4 ...", "application/pdf")},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"


async def test_the_ui_origin_may_still_write(api: httpx.AsyncClient, sample_pdf: bytes) -> None:
    response = await api.post(
        "/v1/documents", **_upload(sample_pdf, "allowed.pdf", ip=_fresh_ip(), origin=ALLOWED_ORIGIN)
    )
    assert response.status_code == 202


async def test_a_request_with_no_origin_is_untouched(
    api: httpx.AsyncClient, sample_pdf: bytes
) -> None:
    """curl, the n8n workflow and every server-to-server caller send no Origin.

    They were never the threat the guard exists for — a page in someone else's tab
    spending this API's ingestion budget from a visitor's IP address is.
    """
    response = await api.post(
        "/v1/documents", **_upload(sample_pdf, "noorigin.pdf", ip=_fresh_ip())
    )
    assert response.status_code == 202


async def test_a_cross_origin_read_is_allowed(api: httpx.AsyncClient) -> None:
    """Reads disclose nothing a caller could not fetch server-side.

    There is no authentication and no cookie to ride on, so blocking cross-origin
    reads would cost embedding and buy nothing. The guard covers writes only, and
    that boundary is deliberate rather than an oversight.
    """
    response = await api.get(
        "/v1/stats", headers={"Origin": "https://evil.example", "X-Forwarded-For": _fresh_ip()}
    )
    assert response.status_code == 200


async def test_rate_limit_is_scoped_to_the_client(
    api: httpx.AsyncClient, sample_pdf: bytes
) -> None:
    """One noisy client must not lock everyone else out."""
    noisy = _fresh_ip()
    for index in range(12):
        await api.post("/v1/documents", **_upload(sample_pdf, f"n{index}.pdf", ip=noisy))

    quiet = await api.get("/health", headers={"X-Forwarded-For": _fresh_ip()})
    assert quiet.status_code == 200


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_unknown_document_returns_a_typed_404(api: httpx.AsyncClient) -> None:
    response = await api.get(
        f"/v1/documents/{uuid.uuid4()}/status", headers={"X-Forwarded-For": _fresh_ip()}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_malformed_uuid_returns_a_typed_422(api: httpx.AsyncClient) -> None:
    response = await api.get(
        "/v1/documents/not-a-uuid/status", headers={"X-Forwarded-For": _fresh_ip()}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_stats_shape_is_stable(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/stats", headers={"X-Forwarded-For": _fresh_ip()})
    assert response.status_code == 200
    body = response.json()
    for key in (
        "documents_total",
        "documents_processed",
        "anomalies_open",
        "avg_latency_ms",
        "p95_latency_ms",
        "est_cost_usd",
        "vendor_spend",
        "llm_mode",
        "router_model",
        "extractor_model",
    ):
        assert key in body


async def test_anomaly_resolution_rejects_an_unknown_id(api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"/v1/anomalies/{uuid.uuid4()}/resolve",
        json={"action": "approve"},
        headers={"X-Forwarded-For": _fresh_ip()},
    )
    assert response.status_code == 404


async def test_anomaly_resolution_rejects_an_invalid_action(api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"/v1/anomalies/{uuid.uuid4()}/resolve",
        json={"action": "delete-everything"},
        headers={"X-Forwarded-For": _fresh_ip()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
