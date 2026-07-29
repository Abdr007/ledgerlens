# syntax=docker/dockerfile:1.7
#
# LedgerLens API container.
#
# Targets Hugging Face Spaces (which requires port 7860) and Google Cloud Run
# (which injects $PORT) from the same image — the entrypoint honours $PORT and
# falls back to 7860.
#
# Build from the repository root:
#   docker build -f infra/Dockerfile -t ledgerlens-api .

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into a self-contained virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependency metadata first, so a source-only change does not re-resolve the
# dependency graph on every rebuild.
COPY apps/api/pyproject.toml ./
COPY apps/api/app/__init__.py ./app/__init__.py

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install "." "langfuse>=2.53"

COPY apps/api/app ./app
RUN /opt/venv/bin/pip install --no-deps . \
 # Strip what a runtime image never executes: vendored test suites, bundled
 # sample data and bytecode caches. Worth ~150 MB, which is cold-start time on
 # a scale-to-zero Cloud Run service.
 && find /opt/venv -type d -name "__pycache__" -prune -exec rm -rf {} + \
 && find /opt/venv -type d -name "tests" -prune -exec rm -rf {} + \
 && find /opt/venv -type d -name "test" -prune -exec rm -rf {} + \
 && find /opt/venv -type f -name "*.pyc" -delete \
 && rm -rf /opt/venv/lib/python3.12/site-packages/pandas/tests \
           /opt/venv/lib/python3.12/site-packages/numpy/_core/tests

# ---------------------------------------------------------------------------
# Runtime — no build tools, no source of the builder stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="LedgerLens API" \
      org.opencontainers.image.description="Intelligent document processing and financial anomaly detection" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=7860 \
    ENVIRONMENT=prod \
    LOG_LEVEL=INFO \
    HOME=/home/app

# curl is the healthcheck; everything else the app needs is a wheel.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app apps/api/app ./app
COPY --chown=app:app apps/api/pyproject.toml ./

# `app/devtools` renders fixture documents with reportlab and Pillow, which are
# dev-only extras and are deliberately absent from the runtime image. Shipping
# a package that cannot import is worse than not shipping it: it turns a clear
# "not installed" into a confusing partial failure. The seed script and the
# evaluation harness run from a developer machine, not from this container.
RUN rm -rf ./app/devtools

# Never run as root: a container that only reads uploads and writes to Postgres
# has no reason to hold uid 0.
USER app

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-7860}/health" || exit 1

# Cloud Run sets $PORT; Hugging Face Spaces expects 7860. One image serves both.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
