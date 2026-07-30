# Deploying LedgerLens

The API runs on a **Hugging Face Docker Space**; the UI is a static Next.js build on
**Vercel**; the ledger is **Neon Postgres**. This document is the whole path, including the
two steps whose failure modes are silent.

> **Docker Spaces require a PRO subscription ($9/month).** Only *Static* Spaces are free,
> and a static host cannot run a Python API. One PRO subscription covers every Docker Space
> on the account, so the marginal cost of this project is zero if you already have one.

| | Hugging Face Docker Space | Render free (previous host) |
|---|---|---|
| Cost | **$9/month** PRO, any number of Spaces | $0 |
| Sleeps when idle | after **48 hours** | after **~15 minutes** |
| Cold start | ~30 s | **52 s** — long enough to read as broken |
| Card required | yes (PRO) | no |

That cold-start row is the entire reason for the move. A demo link that takes 52 seconds to
answer has already lost the room. See [AUDIT.md §4c](../AUDIT.md) and **D-2** in
[SPEC-CONFORMANCE.md](SPEC-CONFORMANCE.md).

The image honours `$PORT` and falls back to 7860, so nothing here is Spaces-specific
except this document.

---

## 1. Create a write token

<https://huggingface.co/settings/tokens> → **Create new token** → type **Write** → copy it.

Do this in *your own* terminal, so the token is never pasted into a chat transcript:

```bash
cd ~/ledgerlens
apps/api/.venv/bin/hf auth login --token hf_YOUR_TOKEN_HERE --add-to-git-credential
```

`--add-to-git-credential` matters: the deploy pushes over HTTPS, and without it git prompts
for a password the Hub no longer accepts.

(`huggingface-cli` still appears in a lot of documentation. It is deprecated as of
`huggingface_hub` 1.x and refuses to run — `hf` is the replacement.)

Verify:

```bash
apps/api/.venv/bin/python -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])"
```

---

## 2. Push, wait for CI, *then* deploy

In that order, and not in one breath.

```bash
git push origin main
gh run watch --exit-status      # all four jobs
make deploy-space
```

`make check`-style gates never build an image. The **`container`** job is the only thing
that catches a Dockerfile or `.dockerignore` mistake, and deploying before it goes green
means replacing a working deployment with one nothing has built. `make deploy-space` refuses
a dirty working tree for the same reason: a deploy ships what has been committed and gated,
not whatever is lying around.

The script is idempotent — run it again to redeploy. It creates the Space if it is missing,
applies the non-secret configuration, pushes the deployable tree, and prints the URL.

---

## 3. What the Space actually builds

Two mechanisms are worth understanding, because both have failed here.

**Configuration comes from `.hf-space.yml`, not from the README.** Spaces reads its
configuration from YAML front matter at the top of `README.md`. Committing that front matter
would make GitHub render a metadata table above the project's own title, so it lives in
`.hf-space.yml` and is prepended to the README on the deploy commit only.

**The Dockerfile is copied to the root on the deploy commit.** Spaces builds `./Dockerfile`
and offers no way to point it elsewhere. The canonical copy lives in `infra/Dockerfile`
next to the rest of the deployment config, so the deploy writes a root copy — and
`.gitignore` lists `/Dockerfile` to keep that generated file off every GitHub commit.

Those two facts collide, and did: `git add -A` honours `.gitignore`, so the first deploy
pushed a Space with no Dockerfile in it and the Hub reported `NO_APP_FILE` with nothing to
explain it. The fix is `git add -f`, plus a `git ls-tree` check on the deploy commit before
the push. `apps/api/tests/test_deploy_space.py` pins both ends: that the root Dockerfile is
ignored, and that the deploy therefore force-adds it.

The deploy is a single orphan commit carrying the current tree minus `SPACE_EXCLUDE`
(`docs/`, `eval/testset/`). The Hub scans a pushed *history* for binary files, not just the
tip, so a screenshot committed weeks ago would otherwise reject today's push.

The build takes **3–5 minutes**. Watch it under the **Logs** tab.

---

## 4. Configuration and secrets

**Non-secret configuration is in version control** — `SPACE_VARIABLES` in
[`scripts/deploy_space.py`](../scripts/deploy_space.py) — and is applied on every deploy, so
the Space's settings page mirrors a reviewable file rather than being the only record of
what production runs. `test_space_variables_validate_against_settings` builds a real
`Settings` from it, so a value production would reject fails a test rather than a remote
build.

**Secrets are set by hand, once.** Space → **Settings** → **Variables and secrets** →
**New secret**:

| Name | Value |
|---|---|
| `DATABASE_URL` | the Neon **pooled** connection string, including `?sslmode=require` |
| `ANTHROPIC_API_KEY` | `sk-ant-…`. Leave unset and the deterministic offline engine runs |
| `LANGFUSE_PUBLIC_KEY` | Langfuse → Settings → API Keys. Optional |
| `LANGFUSE_SECRET_KEY` | same. Optional |

They are never in the image, the repo, or a build log.

> **Without `DATABASE_URL` the Space crash-loops, it does not degrade.** `/health` reports
> `degraded` when the database *goes away*, but a process that starts without one exits
> before serving anything — `wait_for_database` exhausts its retries and `lifespan` raises.
> The log line is `database_unreachable`. That is deliberate: failing fast on a
> misconfiguration beats serving a service that cannot do its job.

---

## 5. Point the web app at it

**Set the environment variable before the first build, not after.** `next.config.ts` derives
the CSP's `connect-src` from `NEXT_PUBLIC_API_BASE_URL` at *build* time. Deploy without it
and the shipped policy pins `connect-src` to `http://localhost:7860`, so every request from
the browser is blocked by the page's own policy — which looks exactly like an API outage and
is invisible in the API's logs, because no request ever leaves the browser.

```bash
cd apps/web
vercel link --yes --project ledgerlens
printf 'https://Abdr007-ledgerlens.hf.space' | vercel env add NEXT_PUBLIC_API_BASE_URL production
vercel --prod --yes
```

Changing the API URL later means **rebuilding**, not just updating the variable.

**Turn off Vercel deployment protection.** New projects default to Vercel Authentication,
which 302s every visitor to a login page — including the interviewer you sent the link to.
Project → Settings → Deployment Protection → Vercel Authentication → Disable.

---

## 6. Verify

```bash
make verify-hosted
```

Eleven checks, and it warms the Space and the Neon compute at the same time. It must end
with `all 11 checks passed · the Space is warm · ready to present`.

It covers the two failures that leave no trace in the API's logs — a UI that redirects to a
login page, and a CSP naming the wrong host — plus the ones that only a real deployment can
show: that the KPI cards and the ledger agree, that no document sits in the ledger
contradicting its own verification result, that identical bytes are deduplicated, and that
the ingestion rate limit actually **binds** rather than merely being configured.

That last one is not theoretical. On the previous host it did not bind: `TRUSTED_PROXY_COUNT`
was one lower than the host's real proxy depth, so the limiter keyed on the address of
whichever edge node answered and 14 uploads in a few seconds all returned `202` while every
response advertised `X-RateLimit-Limit: 10`. See AUDIT.md §4c defect 5.

**`TRUSTED_PROXY_COUNT` must be measured against this host, not carried over.** If it is
wrong, the API logs `proxy_depth_mismatch` once, naming the value to set:

```
proxy_depth_mismatch  observed_hops=2  trusted_proxy_count=1
  fix: set TRUSTED_PROXY_COUNT=2
```

Set it in `SPACE_VARIABLES` and redeploy — not in the settings page, or the next deploy
overwrites it.

> **CORS headers are not yours on this host — measured, not inherited.** Hugging Face
> answers the pre-flight at its edge and echoes whatever `Origin` it was sent. The same
> image refuses `evil.example` when run locally and permits it through the Space, and the
> tell is `access-control-allow-methods: POST`, an echo of the request, where the
> application would have answered `GET, POST, OPTIONS`.
>
> `ALLOWED_ORIGINS` is therefore enforced by the application and overwritten on the way
> out. That is why cross-origin **writes** are refused in the server by
> `OriginGuardMiddleware` with a typed `403 forbidden_origin`, and why
> `make verify-hosted` asserts *that* rather than reading a header something upstream
> rewrites. Full analysis, including what it did and did not expose, in AUDIT.md §4c
> defect 6.

---

## 7. Before a demo, warm it

A Space sleeps after 48 hours idle, and the Neon compute suspends sooner.

```bash
make verify-hosted
```

Run it a minute or two before you present, not the night before. It is the same command as
§6 — warming is a side effect of checking, which is the point: the thing you run to be
confident is the thing that pays the cold start.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Space stuck at `NO_APP_FILE` | The deploy commit carries no root `Dockerfile` | Should be impossible — `deploy_space.py` asserts it before pushing. If it happens, check `.gitignore` and the `git add -f` line |
| Space `RUNTIME_ERROR`, logs show `database_unreachable` | `DATABASE_URL` unset or wrong | §4. It crash-loops until fixed, then recovers by itself |
| `/health` says `degraded` | Database was reachable and now is not | Neon compute suspended, or the connection string rotated |
| Build fails | Read the Logs tab | The `container` CI job builds the same image on every push, so this should have failed there first |
| Push rejected, "YAML metadata" | `short_description` over 60 chars | `.hf-space.yml`. `test_deploy_space.py` catches this before a push |
| Push rejected, "binary files" | A binary reached the deploy tree | Add its directory to `SPACE_EXCLUDE` |
| Push rejected, 401/403 | Read-only token | §1 — the token must be **Write** |
| UI loads, every call hangs | CSP `connect-src` names the old host | §5. Rebuild, do not just change the variable |
| UI loads, CORS error | Origin not in `ALLOWED_ORIGINS` | `SPACE_VARIABLES`, then `make deploy-space` |
| Link sends viewers to a Vercel login | Deployment protection | §5 |
| Uploads never hit `429` | `TRUSTED_PROXY_COUNT` too low | §6 — the API logs the correct value |
| First request takes ~30 s | Space asleep | Expected. §7 |
