# Demo script

Seven minutes, six moves, no surprises. Every number below is measured and reproducible —
see [AUDIT.md](AUDIT.md) for how.

The thing being demonstrated is not "an LLM reads an invoice." Everyone has that. It is
**a system that knows when its own extraction cannot be trusted, and refuses to commit it.**

---

## Before you present — 60 seconds

**Presenting remotely, or sending a link?** Use the deployment. Nothing needs to run on
your machine.

```bash
make verify-hosted    # checks the live stack AND warms it
```

Must end with `all 11 checks passed · the Space is warm · ready to present`. Run it a
minute or two before the call, not the night before: the Space sleeps after 48 hours idle
and the Neon compute suspends sooner, and this is what pays that cost instead of your first
document doing it in front of someone.

| | |
|---|---|
| UI | <https://ledgerlens-jet.vercel.app> |
| API | <https://Abdr007-ledgerlens.hf.space> |

It checks the things that fail *silently* on a hosted deploy — a UI redirecting visitors to
a login page, and a CSP that blocks every call from inside the browser. Neither appears in
the API logs, because in both cases no request ever reaches the API.

It also sweeps every document in the live ledger against the product's own claim: nothing
`DONE` may have a failed check or a high-severity flag against it, and nothing may sit in
review with nothing failing. If that assertion ever breaks, you want to know before the
call, not during it.

**Presenting from your laptop:**

```bash
make up && make seed && make dev     # Postgres, 30 invoices, API :7860 + UI :3000
```

Open <http://localhost:3000>. Check the header badges:

| Badge | Meaning |
|---|---|
| `LEDGER ONLINE` | The database is reachable |
| `CLAUDE LIVE` | A key is set — the vision lane is real |
| `OFFLINE ENGINE` | No key — deterministic rule-based extraction, **labelled as such in the UI** |

Both modes are honest demos. Offline is not a degraded fallback pretending to be online;
it says what it is, and every trace is stamped `mode: "offline"`. If asked, that is the
point: the system never presents a weaker mode as if it were the stronger one.

---

## The run of show

### 1 · Open on the failure, not the happy path (75s)

Scroll straight to the **ledger**, top row: `hosted-verification.jpg`.

Vendor: `—`. Total: `—`. Status: **NEEDS REVIEW**. Lane: vision.

That is a real phone photo of an invoice that the current mode could not read. Say:

> Every demo you have seen today would have shown you a vendor name and a total here. A
> vision model handed an unreadable page will produce plausible fields — it is trained to
> be helpful. This one produced nulls, failed five presence checks, and routed to a human.
> The column is empty because the truth is empty.

Then open its **audit trail**. Six stages, timestamped, including `validation_failed` with
the five rules that failed named individually.

That reframes everything after it. Do not rush this — it is the only move in the whole
demo that a competitor cannot copy in an afternoon.

### 2 · Now show it working (90s)

Drag in a clean invoice PDF. Watch the six-stage rail: **Ingest → Route → Extract →
Validate → Screen → Ledger**, each node lighting as the *audit log* records it.

> Those stages are not a client-side timer. The UI polls `/v1/documents/{id}/status`, and
> the states are projected from the append-only audit log. If the backend stalls at
> extract, the rail stalls at extract.

Point at three things on the result card:

- **The lane it chose.** Text lane for a digital PDF — PyMuPDF, free, zero tokens. The
  routing model decides the document *type*; whether a text layer exists is measured, not
  guessed, because that is a fact you can get for free and a model can get wrong.
- **`is_valid: true`** and the individual checks — `line_items_sum_to_subtotal`,
  `subtotal_plus_tax_equals_total`, `uae_vat_rate`. Each carries expected vs observed.
- **Latency.** ~360 ms end to end, in region.

### 3 · The duplicate (60s)

Open the review queue. The `Possible duplicate payment` card:

> Same vendor, amount within 1%, dates within seven days. Vendor matching is fuzzy —
> rapidfuzz `token_set_ratio` at 88 — because "Gulf Metals L.L.C." and "Gulf Metals LLC"
> are the same supplier and an exact-match join would miss every real duplicate.

This is the one that pays for the project. Duplicate payments are a real line item in every
AP department's losses.

### 4 · The outlier, and why the *reason* matters (60s)

The `Amount outlier` card on `EV-GM-999.pdf`:

> Amount is 86.8 standard deviations higher than normal for Gulf Metals L.L.C.: this
> invoice is 65,333.31 AED against an average of 16,822.50 across 5 prior invoices
> (3.9× the usual).

Read it aloud, then say:

> No finance person will action "anomaly score 86.8". They will action that sentence. Every
> flag carries a severity, a score, the evidence dictionary behind it, and a plain-English
> reason — and the reason is generated by the same pandas code that computed the score, so
> it cannot drift away from the number it explains.

Point at the evidence rows: vendor average, z-score, history size. Then note the guard:

> The detector needs four readable priors before it will fire a z-score at all, and a
> dispersion floor stops a vendor who always bills exactly the same amount from generating
> a false positive on a two-dirham difference.

### 5 · Prove the audit log is real (45s)

Open the audit drawer on any document. Eleven immutable events, the model calls with token
counts, and the line at the bottom:

> *This log is append-only. A database trigger rejects UPDATE and DELETE, so history cannot
> be rewritten even from a direct SQL session.*

If they are technical, offer the receipt:

```sql
UPDATE audit_log SET payload = '{}' WHERE id = 1;
-- ERROR:  audit_log is append-only; UPDATE is not permitted
```

> An append-only log enforced in application code is a convention — anyone with a database
> URL can walk around it. Enforced by a trigger, it survives someone with `psql` and a bad
> afternoon. That is the difference between a feature and a control.

### 6 · Upload the same file twice (45s)

Drag the exact same PDF in again. It returns immediately, no reprocessing, same document id.

> The SHA-256 of the bytes is the idempotency key, and it is a `UNIQUE` constraint with
> `INSERT … ON CONFLICT` — not a read-then-write. Two browser tabs racing the same file
> interleave a read-then-write and you get two rows. Here the database decides the winner
> and the application does not get a vote. There is a test that fires eight concurrent
> uploads and asserts exactly one row.

---

## The numbers (if you get to them)

| Metric | Result |
|---|---|
| Field-level accuracy | **100.0%** (63/63) |
| Line-item accuracy | **100.0%** (23/23) |
| Anomaly precision / recall / F1 | **100% / 100% / 1.00** |
| Tests | **137**, real PostgreSQL, 0 skipped |
| Live latency | avg **399 ms**, p95 **536 ms** |

Say the scope out loud before they ask:

> Those are offline-baseline numbers over the seven of ten documents that mode can read.
> The other three are scans with no text layer — the harness excludes them and names them,
> because a mode that cannot read a document should not be scored as if it did.

---

## The three questions you will be asked

**"How do you know the model isn't hallucinating the numbers?"**
Because the model is never asked for a number that can be derived. It transcribes what is
printed, and it is explicitly forbidden from fixing arithmetic. Pure Python then re-does
every sum in `Decimal` with documented tolerances. If the model invents a total, the
totals check fails and the document goes to review. The verification is not a second
opinion from the same source — it is a different kind of thing entirely.

**"What if the invoice's own maths is wrong?"**
Then it fails validation and a human sees it — which is the correct outcome. This is why
the prompt forbids the model from correcting arithmetic: a model that silently fixes a
broken invoice destroys the exact signal the product exists to catch. That is a design
decision, and it is in the README table.

**"What would you do next?"**
Answer honestly and specifically:
- The vision lane's live accuracy is **not yet measured** — the harness, labels and scoring
  are complete and exercised, but there was no API key at build time. It is reported as
  pending, never as a number. That is one command away.
- Validation tolerances are derived but not *calibrated* against a labelled set of real
  arithmetic errors. Today they are reasoned from rounding behaviour, which is defensible
  and not the same as measured.
- The anomaly detector is per-vendor and unsupervised. With feedback from the review
  queue — approve/reject is already written back to the database — it should become
  supervised, and the z-score threshold should be fitted rather than set at 2.0.

---

## If something breaks

| Symptom | Cause | Fix on the spot |
|---|---|---|
| First request takes ~30 s | Space asleep (48 h idle) or Neon compute suspended | Expected, and it is warm afterwards. `make verify-hosted` exists so this never happens live |
| Link sends the viewer to a Vercel login | Deployment protection re-enabled | Project → Settings → Deployment Protection → disable. Caught by `make verify-hosted` |
| UI loads but every call hangs | CSP `connect-src` pinned to the old host — `NEXT_PUBLIC_API_BASE_URL` was missing or stale at **build** time | Set it, then redeploy. Nothing appears in the API logs because no request leaves the browser |
| UI loads, calls fail with a CORS error | The UI origin is not on `ALLOWED_ORIGINS` | It is in `SPACE_VARIABLES`; re-run `make deploy-space` |
| Health says `degraded` | The database is unreachable | The API is up and saying so. Check `DATABASE_URL` on the Space |
| `429 Too Many Requests` | Rate limiter, 10 uploads/min/IP | Wait a minute. Reads have their own 600/min ceiling, so the dashboard keeps working |
| Badge says `OFFLINE ENGINE` | No `ANTHROPIC_API_KEY` | Expected without a key. Say so; the UI already labels it |

Never explain a result you did not expect. Say "that's not what it does normally, let me
show you the evaluation" and go to the numbers — they are reproducible and the improvised
explanation is not.

---

## One-line summary, if you get 10 seconds

> Vision-LLM document extraction where the model transcribes and pure Python verifies —
> it is forbidden from fixing arithmetic, every sum is re-derived in `Decimal`, and
> anything that fails routes to a human review queue instead of the database, on an
> append-only audit trail a database trigger will not let you rewrite.

---

# The video — 2:30, no cuts

A recorded demo is not a live one slowed down. Nobody scrubs back, so every claim has to
land the first time, and dead air while a model thinks reads as a broken app. Run
`make verify-hosted` first: it warms the Space and the database, so the first upload in the
take is not the slow one.

Record at 1512×1100, one continuous take. Retakes are cheaper than cuts — a cut in a demo
of a system that claims to be auditable looks like something was removed.

**Have ready on the desktop:** one clean invoice PDF, and the same file a second time.

| Time | On screen | What you say |
|---|---|---|
| **0:00–0:10** | Dashboard, resting. Do not touch anything. Let the KPI counters finish. | "Intelligent document processing with one rule: the model extracts, code verifies. It is never allowed to check its own arithmetic." |
| **0:10–0:40** | Scroll to the ledger. Hold on the **top row** — `hosted-verification.jpg`, vendor `—`, total `—`, **NEEDS REVIEW**. | "Start with the failure. This is a phone photo the system could not read. Every demo you have seen would show you a plausible vendor and a plausible total here, because a vision model handed an unreadable page will produce them. This one produced nulls, failed five checks, and routed to a human. The column is empty because the truth is empty." |
| **0:40–0:55** | Open its audit trail. Point at the six stages and `validation_failed`. | "And it says exactly why — five presence checks, named individually, on an append-only trail." |
| **0:55–1:25** | Drag in the clean invoice. Let the six-stage rail run. Do not talk over the animation. | "Now the same pipeline on something readable. Ingest, route, extract, validate, screen, ledger. Those nodes are not a timer — the UI polls the API and the states are projected from the audit log, so if the backend stalls, the rail stalls." |
| **1:25–1:45** | Result card: lane, the individual validation checks, latency. | "Text lane, because PyMuPDF *measured* a text layer rather than asking a model to guess. Line items sum to subtotal, subtotal plus tax equals total, VAT is 5% — each with expected and observed. Three hundred and sixty milliseconds." |
| **1:45–2:05** | Review queue. Read the amount-outlier reason aloud, in full. | "Eighty-six standard deviations above normal for this vendor: sixty-five thousand against an average of sixteen thousand eight hundred across five prior invoices. Nobody actions a score of 86.8. They action that sentence — and the same code produced both, so the explanation cannot drift from the number." |
| **2:05–2:20** | Audit drawer. Hold on the append-only footer line. | "A database trigger rejects UPDATE and DELETE. Not application code — a trigger. This survives someone with a database URL and a bad afternoon." |
| **2:20–2:30** | Drag the **same** file in again. It returns instantly, same id, no reprocessing. | "Same bytes, same SHA-256, same record. It is a unique constraint with insert-on-conflict, so two tabs racing the same file cannot make two rows. The database decides; the application does not get a vote." |

**The shot that sells it is 0:10–0:40.** Everything after it is a competent pipeline.
The empty column is the part almost nobody else can show, so give it room and do not talk
over the moment the viewer reads `—`.

**If you record only 30 seconds:** the unreadable document routing to review, then the same
file uploaded twice returning one record. That pair is the whole thesis — a system that
declines to guess, and a system that cannot be made to double-count.

**Do not** record with a key configured and then quote the offline numbers, or imply the
100% field accuracy covers the three scans it explicitly excludes. The entire point of the
project is that it does not overclaim; a demo that does is worse than no demo.
