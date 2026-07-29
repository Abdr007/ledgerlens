# LedgerLens — one entry point for every check in AUDIT.md.
#
#   make setup     install both toolchains
#   make up        start Postgres
#   make seed      load 30 invoices of vendor history
#   make dev       run the API and the UI
#   make audit     every gate: ruff, mypy, pytest, eslint, tsc, build

SHELL       := /bin/bash
API         := apps/api
WEB         := apps/web
PY          := $(API)/.venv/bin/python
PIP         := $(API)/.venv/bin/pip
RUFF        := $(API)/.venv/bin/ruff
MYPY        := $(API)/.venv/bin/mypy
PYTEST      := $(API)/.venv/bin/pytest
# Compose ships two ways: the v2 CLI plugin (`docker compose`) and the standalone
# binary (`docker-compose`). Detect which one is installed rather than assuming.
COMPOSE_BIN := $(shell docker compose version >/dev/null 2>&1 && echo 'docker compose' || echo 'docker-compose')
COMPOSE     := $(COMPOSE_BIN) -f infra/docker-compose.dev.yml

API_PORT    ?= 7860
WEB_PORT    ?= 3000

.DEFAULT_GOAL := help
.PHONY: help setup setup-api setup-web up down logs psql seed reset dev dev-api dev-web \
        lint fmt typecheck test test-unit eval web-lint web-typecheck web-build audit \
        docker-build clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Setup -----------------------------------------------------------------

setup: setup-api setup-web ## Install both toolchains

setup-api: ## Create the API virtualenv and install dependencies
	@command -v uv >/dev/null 2>&1 \
	  && (cd $(API) && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev,docgen,observability]') \
	  || (cd $(API) && python3.12 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e '.[dev,docgen,observability]')

setup-web: ## Install web dependencies
	cd $(WEB) && npm install

# --- Local stack -----------------------------------------------------------

up: ## Start PostgreSQL
	$(COMPOSE) up -d postgres
	@until docker exec ledgerlens-postgres pg_isready -U ledgerlens >/dev/null 2>&1; do sleep 1; done
	@echo "PostgreSQL ready on localhost:5433"

down: ## Stop the local stack
	$(COMPOSE) down

logs: ## Tail the stack logs
	$(COMPOSE) logs -f

psql: ## Open a psql shell on the local database
	docker exec -it ledgerlens-postgres psql -U ledgerlens -d ledgerlens

# --- Data ------------------------------------------------------------------

seed: ## Load 30 invoices across 6 vendors, incl. a planted near-duplicate
	cd $(API) && ../../$(PY) scripts/seed.py --reset

reset: ## Empty the ledger without reseeding
	docker exec ledgerlens-postgres psql -U ledgerlens -d ledgerlens -c \
	  "TRUNCATE anomalies, extractions, llm_traces, audit_log, failed_jobs, documents RESTART IDENTITY CASCADE;"

# --- Run -------------------------------------------------------------------

dev: ## Run the API and the UI together
	@$(MAKE) -j2 dev-api dev-web

dev-api: ## Run the API with reload
	cd $(API) && ../../$(PY) -m uvicorn app.main:app --reload --port $(API_PORT)

dev-web: ## Run the Next.js dev server
	cd $(WEB) && npm run dev -- --port $(WEB_PORT)

# --- Gates -----------------------------------------------------------------

lint: ## ruff check + format check (API)
	cd $(API) && ../../$(RUFF) check .
	cd $(API) && ../../$(RUFF) format --check .

fmt: ## Apply ruff formatting
	cd $(API) && ../../$(RUFF) format . && ../../$(RUFF) check --fix .

typecheck: ## mypy --strict (API)
	cd $(API) && ../../$(MYPY) app scripts

test: ## pytest, including integration against real PostgreSQL
	cd $(API) && ../../$(PYTEST) -q

test-unit: ## pytest, skipping tests that need a database
	cd $(API) && ../../$(PYTEST) -q -m "not integration"

eval: ## Score the labelled test set and print resume numbers
	$(PY) eval/run_eval.py

web-lint: ## eslint (web)
	cd $(WEB) && npm run lint

web-typecheck: ## tsc --noEmit (web)
	cd $(WEB) && npm run typecheck

web-build: ## Production build (web)
	cd $(WEB) && npm run build

audit: lint typecheck test web-typecheck web-lint web-build ## Every gate in AUDIT.md
	@echo
	@echo "  All gates passed: ruff · mypy --strict · pytest · tsc · eslint · next build"

# --- Deploy helpers --------------------------------------------------------

docker-build: ## Build the API container
	docker build -f infra/Dockerfile -t ledgerlens-api:latest .

clean: ## Remove caches and build output
	rm -rf $(API)/.mypy_cache $(API)/.ruff_cache $(API)/.pytest_cache $(WEB)/.next
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
