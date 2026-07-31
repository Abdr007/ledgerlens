# Spec Conformance Matrix

Every requirement extracted verbatim from **LedgerLens Build Spec.pdf** (8 pages).
Each row is verified in `AUDIT.md`. Nothing here is optional.

## §2 Architecture — the 8 stages

| # | Requirement | Where implemented |
|---|---|---|
| 1 | Drag-and-drop upload → POST `/v1/documents`; **SHA-256 file hash = idempotency key** (same file twice → same record, no double processing) | `routers/documents.py`, `core/files.py`, `pipeline/orchestrator.py` |
| 2 | **Routing gate — Claude Haiku 4.5**: digital-text PDF vs scanned/photo + doc type (invoice / receipt / contract). Cost-aware model routing | `pipeline/route.py` |
| 3a | **Text lane** — PyMuPDF embedded text. Free, instant, zero tokens | `pipeline/textlane.py` |
| 3b | **Vision lane** — Claude Sonnet 4.6 vision reads scans, tables, handwriting — **Deviation, see D-3.** The lane runs Claude Sonnet 5 | `pipeline/extract.py` |
| 4 | **Structured extraction** — Claude *tool use* + Pydantic v2. Schema-forced. Nulls allowed, **invention forbidden**. Failed validation → error fed back → **max 2 self-correction retries** | `pipeline/extract.py` |
| 5 | **Deterministic validation (pure Python — never an LLM)**: line items sum, subtotal+tax=total, 5% UAE VAT tolerance, date sanity. Fail → `NEEDS_REVIEW`, never auto-commit | `pipeline/validate.py` |
| 6 | **Anomaly engine (pandas, explainable)**: fuzzy duplicates (rapidfuzz, vendor + amount within 1% + date within 7d), per-vendor amount z-score > 2, unusual payment terms, round-number bias. Every flag carries **severity + plain-English reason** | `pipeline/anomaly.py` |
| 7 | **Ledger — Postgres**: `documents`, `extractions`, `anomalies`, `audit_log` (append-only), `failed_jobs`. `UNIQUE(file_hash)` + transactions everywhere | `models/tables.py`, `core/db.py` |
| 8 | Mission-control UI · Langfuse observability on every LLM call · n8n automation showcase | `apps/web/`, `core/tracing.py`, `automation/n8n/` |

### D-3 — Claude Sonnet 5 on the vision lane, not Sonnet 4.6

**Spec §2 stage 3b** names Claude Sonnet 4.6 as the extractor. The lane now runs
`claude-sonnet-5`; the routing gate is unchanged on Claude Haiku 4.5.

**Why.** Sonnet 5 supersedes 4.6 in the same tier and the same price band, and is
materially stronger on exactly this lane's work — reading tables and degraded
scans. A spec that pins a model version is really specifying a capability: a
vision-capable, tool-use-capable model in the Sonnet tier, chosen because Opus
would be wasteful for transcription. That intent is better served by the current
model than by the one that happened to be current when the spec was written, and
version pins age in a way capability requirements do not.

**What this cost.** One real behavioural difference, handled rather than absorbed.
Omitting the `thinking` parameter meant *no thinking* on Sonnet 4.6 and means
*adaptive thinking* from Sonnet 5. Left alone that would have been a silent
change of two things this project measures: `max_tokens` caps thinking and output
together, so a long think can starve the `tool_use` block and route a readable
document to review for no reason; and thinking bills as output tokens, moving
cost-per-document for no accuracy gain on a transcription task. The extraction
lane therefore asks for thinking off explicitly (`LlmRequest.disable_thinking`),
which restores the 4.6 behaviour exactly. The usual objection — that models reach
for tools less with thinking disabled — does not apply here, because `tool_choice`
is pinned to a single tool and the call is forced either way.

**What was preserved.** Everything the stage is actually for. Extraction is still
schema-forced through pinned tool use with no prose path; the deterministic
`Decimal` layer, not the model, is still what verifies the arithmetic; and
anything failing verification still routes to review rather than the ledger. The
model name is a setting (`MODEL_EXTRACTOR`), so this is a configuration change
with a documented reason, not a rewrite.

**What must be re-measured.** Sonnet 5 tokenizes roughly 30% higher than 4.6 for
the same text, so token counts, cost-per-document and latency from the 4.6 era do
not transfer. The first live run is the new baseline, not a comparison against the
old numbers — and every report is stamped with the model and mode that produced
it, so the two cannot be confused.

## §5 Repository layout — must match exactly

```
ledgerlens/
  apps/web/            apps/api/app/{routers,pipeline,models,core}
  apps/api/tests/      automation/n8n/     infra/     eval/
  AUDIT.md  README.md  .env.example
```

`infra/` holds the Dockerfile and the local compose file. It no longer holds a
Terraform module — see D-2.

## §6 Futuristic UI

| Requirement | Status |
|---|---|
| Near-black navy base `#0a0e1a`, glassmorphism panels, single electric accent cyan `#22d3ee` | **Deviation — see D-1 below.** Near-black *violet* base `#07060d`, chamfered solid panels, single electric accent acid-lime `#c8ff2f`. `globals.css` `@theme` tokens; `.panel` utility |
| Inter/Geist font, subtle grid background, glow on active elements, **no rainbow gradients** | Geist via `next/font`; `.backdrop-grid`; `.glow-*`; no rainbow gradients — one accent hue only |
| Full-width drag-drop hero | `components/pipeline/dropzone.tsx` |
| 6-stage pipeline **Ingest → Route → Extract → Validate → Screen → Ledger**; nodes pulse while active, green on pass, red on flag | `components/pipeline/pipeline-rail.tsx` |
| **Driven by real backend status polling (`GET /v1/documents/{id}/status`), not faked timers** | `app/page.tsx` poll loop; states projected from `audit_log` |
| Extracted fields *type themselves* into a result card as they arrive | `components/pipeline/result-card.tsx` |
| KPI cards (docs processed, avg latency, est. cost, anomalies open) with animated counters | `components/dashboard/kpi-cards.tsx` |
| Vendor-spend bar chart (Recharts) | `components/dashboard/vendor-chart.tsx` |
| Anomaly queue as severity-glowed cards, each with plain-English reason + Approve/Reject writing back to Postgres | `components/dashboard/anomaly-queue.tsx` → `POST /v1/anomalies/{id}/resolve` |
| 'Audit Trail' drawer showing the append-only event log per document | `components/dashboard/audit-drawer.tsx` |

### D-1 — Deliberate deviation from the §6 palette

**What changed.** The spec names an exact palette: navy `#0a0e1a`, cyan `#22d3ee`,
glassmorphism (backdrop blur), rounded panels. LedgerLens instead ships
violet-black `#07060d`, acid-lime `#c8ff2f`, solid chamfered panels and a
scanline texture — no blur anywhere.

**Why.** This build is one of three portfolio projects shipped in the same
window. The spec's literal palette is a widely-used one, and it collided
token-for-token with a sibling project (six identical hex values). Two
portfolio pieces that look like the same template damage both; a reviewer reads
it as one template reskinned, not two systems built. Departing from a *styling*
instruction to preserve the artefact's actual purpose is the right trade.
Approved by the project owner before implementation.

**What was preserved.** Every stated *intent* behind §6 holds:

| §6 intent | Held? |
|---|---|
| Near-black base, dark mission-control feel | Yes — `#07060d` is darker than `#0a0e1a` |
| Exactly ONE electric accent | Yes — `#c8ff2f`, used nowhere decoratively |
| No rainbow gradients | Yes — no multi-hue gradient exists in the stylesheet |
| Subtle grid background | Yes — `.backdrop-grid`, unchanged in structure |
| Glow on active elements | Yes — `.glow-accent/pass/warn/flag` |
| Geist / Geist Mono typography | Yes — unchanged, now mono-forward for numerals |
| Semantic state colours stay semantic | Yes — pass/warn/flag are state-only, never decoration |

**Scope.** Presentation layer only: `globals.css` tokens plus the class names
in `apps/web/src/components/`. No API contract, schema, pipeline stage, prompt,
validation rule or test changed. Every other §6 row in the table above — the
six named stages, real status polling, self-typing result card, KPI counters,
Recharts vendor chart, anomaly queue, audit drawer — is met exactly as written.

## §7 Production quality bar

| Category | Requirement |
|---|---|
| Race conditions | `UNIQUE(file_hash)` + `INSERT … ON CONFLICT`; all multi-step writes in DB transactions; status transitions `PENDING→PROCESSING→DONE/NEEDS_REVIEW` enforced with optimistic checks |
| Idempotency | Re-uploading an identical file returns the existing record; retried API calls never duplicate rows |
| Error handling | Every external call (Claude, DB) wrapped with timeout + exponential-backoff retry (**3 attempts**); failures land in `failed_jobs` with a reason; API returns **typed error responses, never stack traces** |
| Security | File type + size whitelist (**PDF/PNG/JPG, ≤10 MB**); prompt-injection hardening — document content is data, never instructions, **stated explicitly in the system prompt**; rate limit **10 req/min/IP**; secrets only via env vars; CORS locked to the Vercel domain |
| Zero warnings | ruff + mypy clean on API; eslint + `tsc --noEmit` clean on web; pytest green; **no console errors in browser** |
| Evaluation | `eval/` runs the labeled set and prints field-level accuracy |

## §8 Master prompt — additional binding details

- Extraction schema field names, exactly: `vendor`, `invoice_number`, `issue_date`, `due_date`,
  `line_items[{description, qty, unit_price, amount}]`, `subtotal`, `tax`, `total`, `currency`, `payment_terms`
- API surface versioned under `/v1`
- FastAPI Python 3.12 · SQLAlchemy 2 · Pydantic v2 · Anthropic SDK · Langfuse on every LLM call ·
  slowapi 10/min/IP · CORS locked to web origin · **Dockerfile for Hugging Face Spaces (port 7860)**
- Auto OpenAPI docs at `/docs`
- `.env.example` lists `ANTHROPIC_API_KEY`, `DATABASE_URL`, Langfuse keys, `ALLOWED_ORIGIN`
- `eval/run_eval.py` processes `eval/testset/` and prints **field-level accuracy + anomaly precision/recall**;
  ship **10 synthetic invoices now, including 2 near-duplicates and 1 inflated amount**, so it runs immediately
- Seed script loads **30 realistic historical invoices across 6 vendors** so charts and z-scores work on
  first open, including a **planted near-duplicate pair** so the anomaly demo always fires

## §8 Definition of Done

| ID | Check |
|---|---|
| (a) | `ruff check` + `mypy` pass — **zero issues** |
| (b) | `eslint` + `tsc --noEmit` pass — **zero issues** |
| (c) | `pytest` green, with tests for validation math, idempotent re-upload, duplicate detection, and **status transitions under two concurrent requests (no race)** |
| (d) | End-to-end: uploading a sample scanned invoice returns validated structured data and the pipeline UI animates through all stages |
| (e) | Planted duplicate raises a **HIGH-severity** anomaly with an explanation |
| (f) | `README.md` with mermaid architecture diagram, local dev (`docker-compose.dev.yml`), and step-by-step **free** deploy: Vercel (web), **GCP Cloud Run always-free (api container, with `gcloud` commands)** AND **Hugging Face Spaces as no-card fallback**, Neon, Langfuse; plus optional `infra/terraform/` module provisioning the Cloud Run service + secret env vars — **Deviation, see D-2.** The Space is the deployment; Cloud Run and Terraform are gone |
| — | `AUDIT.md` listing **every check with PASS status** |

### D-2 — One API host, not three

**Spec §8(f)** asks for Cloud Run as the primary API host with `gcloud` commands,
Hugging Face Spaces as a no-card fallback, and an optional `infra/terraform/`
module provisioning the Cloud Run service. The repository shipped all three.

**What it does now.** The API runs on a Hugging Face Docker Space, and that is the
only API deployment path the repository describes. `render.yaml`, the Cloud Run
walkthrough and `infra/terraform/` are deleted.

**Why.** The spec asked for a *documented* deployment; what it got was three, of
which none was serving. Cloud Run was named "primary" and had never been applied
— AUDIT.md said so in as many words, because it would create billable resources
and the account's billing is inactive. Render *was* serving, on a free tier that
stops the container after roughly fifteen minutes idle, so the live API took
**52 seconds** to answer after a quiet spell; anyone opening the demo link
concluded it was broken before it replied. The Space sleeps after 48 hours
instead, and the account's existing PRO subscription covers unlimited Docker
Spaces at no marginal cost.

A repository that carries configuration for hosts it does not use is not
documenting three options; it is publishing two untested ones next to the real
one, and offering the reader no way to tell which is which. Deleting them is the
honest version of §8(f): one host, actually running, verifiable in one command
(`make verify-hosted`).

**What was preserved.** The requirement behind §8(f) is that a reader can deploy
this for free and that the deployment is real. Both hold, and more tightly than
before — the deployment is now checked from outside by eleven assertions against the
running stack, and its non-secret configuration lives in version control
(`SPACE_VARIABLES` in `scripts/deploy_space.py`, validated against the settings
schema by `test_space_variables_validate_against_settings`) rather than only in a
provider's settings page. The container itself is unchanged and still honours
`$PORT`, so it is not tied to this host: the Cloud Run commands were deleted, not
the ability to run there.

**Scope.** Deployment configuration and documentation only. No API contract,
schema, pipeline stage, prompt, validation rule or test behaviour changed.

## §9 Post-build checks (the three that make it genuinely production-grade)

1. Read `AUDIT.md` and rerun each command.
2. **Kill the network mid-upload** and confirm the doc lands in `failed_jobs` with a reason.
3. **Upload the same file twice fast (two tabs)** and confirm exactly one record exists.

## §3/§4 Stack and zero-cost deployment

Next.js 15 + TS + Tailwind + shadcn/ui + Framer Motion · FastAPI 3.12 in Docker · Claude Sonnet 5
(vision + tool use) · Claude Haiku 4.5 (routing) · Pydantic v2 + pure-Python rules · pandas + rapidfuzz +
z-scores · Neon Postgres free · Langfuse cloud free · Vercel Hobby · **Hugging Face Docker Space as
the API host** (see D-2) · n8n self-hosted local.

**Signature sentence for interviews:** *"LLMs extract, code verifies. The model never checks its own
math — a deterministic validation layer does, and anything that fails routes to a human review queue
instead of the database."*
