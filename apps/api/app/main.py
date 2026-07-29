"""FastAPI application factory.

Security posture, all from spec §7:

* **CORS is locked** to `ALLOWED_ORIGIN` — no wildcard, ever, because the API
  accepts uploads and mutates state.
* **Rate limiting** is on, keyed by client IP (see `routers.rate_limit`).
* **Errors are typed.** Every handler below converts a failure into the
  `{"error": {...}}` envelope; an unexpected exception is logged with its
  traceback server-side and returned as an opaque `internal_error`. A stack trace
  never reaches a client.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.bootstrap import init_schema, wait_for_database
from app.core.db import dispose_engine, init_engine
from app.core.errors import ErrorCode, LedgerLensError
from app.core.logging import configure_logging, get_logger
from app.core.settings import get_settings
from app.core.tracing import shutdown_tracer
from app.deps import get_claude_client, shutdown_clients
from app.pipeline.orchestrator import recover_stale_documents
from app.routers import anomalies, documents, health, stats
from app.routers.rate_limit import limiter

logger = get_logger(__name__)

API_TITLE = "LedgerLens API"
API_VERSION = "1.0.0"
API_DESCRIPTION = """
**Intelligent Document Processing & Financial Anomaly Detection.**

Drop in an invoice, receipt or contract as a PDF, scan or phone photo. LedgerLens
routes it to the cheapest lane that can read it, extracts every field into a
schema-validated record, **verifies the arithmetic in pure Python**, screens it
against vendor history for duplicates and outliers, and writes the whole journey
to an append-only audit trail.

> LLMs extract, code verifies. The model never checks its own math — a
> deterministic validation layer does, and anything that fails routes to a human
> review queue instead of the database.
"""


def _error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


# This service returns JSON and nothing else. Locking the content policy all the
# way down costs nothing here and removes a whole class of "the API rendered
# attacker HTML" findings — /docs is exempted below because Swagger UI needs
# scripts and styles to work at all.
_SECURITY_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}
_DOCS_PATHS: Final[frozenset[str]] = frozenset({"/docs", "/redoc", "/openapi.json"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request and apply security headers."""

    def __init__(self, app: Any, *, hsts: bool) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        response = await call_next(request)
        if request.url.path in _DOCS_PATHS:
            # Swagger UI is self-hosted assets + inline styles; a default-src
            # 'none' policy would leave an unusable blank page.
            return response
        response.headers["x-request-id"] = request_id
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down."""
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = init_engine(settings)
    # A serverless database may be suspended; give it a chance to wake before
    # touching the schema, so a cold boot is a slow boot rather than a crash.
    await wait_for_database(engine)
    await init_schema(engine)
    # A previous process may have died mid-document; do not leave those rows
    # spinning in the UI for ever.
    await recover_stale_documents()

    client = get_claude_client()
    logger.info(
        "api_started",
        extra={
            "environment": settings.environment,
            "llm_mode": client.mode,
            "router_model": settings.model_router,
            "extractor_model": settings.model_extractor,
            "langfuse": settings.langfuse_enabled,
            "allowed_origins": settings.allowed_origins,
        },
    )
    try:
        yield
    finally:
        await shutdown_clients()
        shutdown_tracer()
        await dispose_engine()
        logger.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.state.limiter = limiter
    app.add_middleware(RequestContextMiddleware, hsts=settings.environment == "prod")
    # Never `allow_origins=["*"]`: this API takes uploads and mutates state.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-Id"],
        expose_headers=["X-Request-Id", "Retry-After"],
        max_age=600,
    )

    # ---- Typed error handlers -------------------------------------------

    @app.exception_handler(LedgerLensError)
    async def handle_domain_error(_: Request, exc: LedgerLensError) -> JSONResponse:
        return _error_response(exc.status_code, str(exc.code), exc.message, exc.details)

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        response = _error_response(
            429,
            str(ErrorCode.RATE_LIMITED),
            "Too many requests. Please slow down and try again shortly.",
            {"limit": str(exc.detail)},
        )
        response.headers["Retry-After"] = "60"
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422,
            str(ErrorCode.VALIDATION_ERROR),
            "The request payload was not valid.",
            {
                "issues": [
                    {"field": ".".join(str(p) for p in issue["loc"]), "problem": issue["msg"]}
                    for issue in exc.errors()[:10]
                ]
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        return _error_response(exc.status_code, str(code), str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Logged in full server-side; the client gets an opaque identifier only.
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(
            "unhandled_exception",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return _error_response(
            500,
            str(ErrorCode.INTERNAL_ERROR),
            "An unexpected error occurred. The incident has been logged.",
            {"request_id": request_id},
        )

    # ---- Routes ----------------------------------------------------------
    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(anomalies.router)
    app.include_router(stats.router)

    return app


app = create_app()
