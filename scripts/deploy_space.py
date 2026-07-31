#!/usr/bin/env python3
"""Create the Hugging Face Space and push this repository to it.

Idempotent: run it again to redeploy. It creates the Space if it is missing, commits
anything outstanding, wires the git remote, and pushes.

    python scripts/deploy_space.py --space lexora --public

Authentication comes from `~/.cache/huggingface/token`, written by `hf auth login`
(note: `huggingface-cli` is deprecated in huggingface_hub 1.x and no longer runs).
No token is passed on this command line, so it cannot reach shell history.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# Files that must exist before a deploy is even attempted. If any is missing the
# Space builds and then reports `degraded`, which is a slow way to discover a
# missing artefact.
SPACE_CONFIG: Final = Path(".hf-space.yml")

# Deliberately *not* `Dockerfile`. The canonical copy lives in `infra/Dockerfile`
# next to the rest of the deployment config; the root copy is written onto the
# deploy commit only (see `push_with_space_config`) and is gitignored. Requiring
# it here — as the Lexora original did, where the Dockerfile genuinely sits at the
# root — made this script fail its own preflight and never run.
REQUIRED: Final = (
    Path("README.md"),
    SPACE_CONFIG,
    Path("infra/Dockerfile"),
    Path("apps/api/pyproject.toml"),
)

# The Hub enforces this in a pre-receive hook, so an over-long description rejects the
# push only after the entire repository has been uploaded, and the message names the
# field without naming the file it lives in. Checking it here costs nothing.
SHORT_DESCRIPTION_MAX: Final = 60

#: Non-secret deployment configuration, applied to the Space on every deploy.
#:
#: This lives in the repository rather than only in the Space's settings page for
#: the same reason the Render blueprint it replaces did: a deploy variable is code
#: that only ever executes on the platform, so a typo in it is invisible until
#: production refuses to boot. `ENVIRONMENT: production` — not one of
#: `dev|test|prod` — once killed a container at import with a pydantic error
#: before it served a request. `test_space_variables_validate_against_settings`
#: builds a real `Settings` from this dict, so that fails a test now instead of a
#: remote build three minutes later.
#:
#: Secrets are deliberately absent. This file is public; the day it carries a
#: database password is the day the password is public. They are set once, by
#: hand, in Settings -> Variables and secrets.
SPACE_VARIABLES: Final[dict[str, str]] = {
    # The UI's origin. A domain name is not a secret, so it belongs here where it
    # is reviewable and deploys itself. Never "*": CORS is the only thing stopping
    # another origin from driving this API with a visitor's browser.
    "ALLOWED_ORIGINS": "https://ledgerlens-jet.vercel.app",
    "ENVIRONMENT": "prod",
    # Production connects as `ledgerlens_app`, which holds SELECT/INSERT/UPDATE and
    # nothing else — no ownership, so it cannot disable the audit log's append-only
    # trigger (AUDIT.md §4c, Residual 1). The schema is applied separately, by the
    # owning role, via `scripts/grant_app_role.py --migrate`.
    "DB_MANAGE_SCHEMA": "false",
    "LOG_LEVEL": "INFO",
    "LANGFUSE_HOST": "https://cloud.langfuse.com",
    # How many proxies the platform actually puts in front of the container.
    #
    # This number must be MEASURED, never assumed. Set it too low and the rate
    # limit keys on a proxy's address instead of the caller's, so callers scatter
    # across however many edge nodes answer and the limit never binds — while
    # still reporting `X-RateLimit-Limit` on every response. That is exactly what
    # the previous host was doing: 14 uploads in seconds, all 202. AUDIT.md §4c,
    # defect 5.
    #
    # The API logs `proxy_depth_mismatch` once, naming the value to set here, and
    # `make verify-hosted` fails if the limit does not bind. Confirm against a
    # running deployment before trusting this value.
    "TRUSTED_PROXY_COUNT": "1",
    # Spec §7: 10 uploads/minute/IP on the expensive path. Reads are cheap and the
    # pipeline visual polls about once a second, so they get their own ceiling.
    "RATE_LIMIT": "10/minute",
    "READ_RATE_LIMIT": "600/minute",
    "MAX_UPLOAD_BYTES": "10485760",
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


def frontmatter_problems(frontmatter: str) -> list[str]:
    """Check the Spaces configuration block. Pure, so it is testable without a repo."""
    problems: list[str] = []
    if "sdk: docker" not in frontmatter:
        problems.append("README.md frontmatter does not declare `sdk: docker`")

    for line in frontmatter.splitlines():
        if not line.startswith("short_description:"):
            continue
        # Split once only: quoting is optional in YAML, and a description containing a
        # colon needs the quotes — splitting on every colon would truncate at the first.
        described = line.split(":", 1)[1].strip().strip("\"'")
        if len(described) > SHORT_DESCRIPTION_MAX:
            problems.append(
                f"short_description is {len(described)} characters; the Hub rejects "
                f"anything over {SHORT_DESCRIPTION_MAX}. Shorten it in README.md."
            )
    return problems


def explain_push_failure(stderr: str) -> str:
    """Turn the remote's rejection into the one instruction that acts on it.

    This used to blame a read-only token whatever the remote actually said. The Hub
    rejects a push for several unrelated reasons and always names the one that applies;
    guessing sends you to the tokens page while the real fault sits in README.md. When
    nothing matches, say so rather than inventing a cause.
    """
    if "YAML metadata" in stderr:
        return (
            "push failed: the Hub rejected the repository metadata. Correct the field "
            "named above in README.md's frontmatter and re-run."
        )
    if "binary files" in stderr:
        return (
            "push failed: the Hub refuses binary files outside Xet/LFS. Store the "
            "offending file above in a text format, or track it with Xet."
        )
    if any(marker in stderr for marker in ("Authentication", "401", "403")):
        return (
            "push failed: authentication. Create a WRITE token at "
            "https://huggingface.co/settings/tokens and log in again with "
            "`hf auth login --token hf_... --add-to-git-credential`."
        )
    return "push failed. The remote's reason is above."


#: Never pushed to the Space. Both entries are binary and neither reaches the image
#: (`.dockerignore` drops them from the build context too): `docs/screens` holds the
#: README screenshots, and `eval/testset` holds the degraded scans. The Hub polices
#: binary files across a pushed history rather than only the tip commit, so keeping
#: them out is cheaper than discovering the limit during a deploy.
SPACE_EXCLUDE: Final = ("docs", "eval/testset")


def push_with_space_config(branch: str) -> subprocess.CompletedProcess[str]:
    """Push the deployable tree to the Space as a single synthetic commit.

    A Space is a deployment target, not a mirror of this project's history, and treating
    it as a mirror caused two separate failures. The Hub scans the whole history of a
    pushed ref for binary files, so a PNG committed weeks ago rejects today's push; and
    it reads a Space's configuration from README.md front matter, which GitHub renders as
    a table above the project's own title.

    An orphan commit answers both. It carries the current tree minus `SPACE_EXCLUDE`,
    with the configuration from `.hf-space.yml` prepended to the README — no history to
    scan, and nothing on the GitHub branch changes. The branch is deleted afterwards
    whatever happens, so a failed push cannot leave the repository on it.
    """
    readme = REPO_ROOT / "README.md"
    original = readme.read_text(encoding="utf-8")
    config = (REPO_ROOT / SPACE_CONFIG).read_text(encoding="utf-8").strip()
    temp = "ledgerlens-space-deploy"

    run("git", "branch", "-D", temp, check=False)
    run("git", "checkout", "--orphan", temp)
    try:
        for excluded in SPACE_EXCLUDE:
            run("git", "rm", "-r", "--cached", "--ignore-unmatch", excluded, check=False)
        readme.write_text(f"---\n{config}\n---\n\n{original}", encoding="utf-8")
        # Spaces builds ./Dockerfile from the repository root and offers no way to point
        # it elsewhere. The canonical copy stays in infra/ next to the rest of the
        # deployment config; this places it where the Hub will look, on the deploy
        # commit only.
        (REPO_ROOT / "Dockerfile").write_text(
            (REPO_ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8"), encoding="utf-8"
        )
        run("git", "add", "-A")
        # `-f` because `.gitignore` lists `/Dockerfile` — deliberately, since the root
        # copy is generated and must never land on a GitHub commit. Plain `git add -A`
        # honours that and silently drops the one file the Space build reads, which
        # produces a Space stuck at NO_APP_FILE and no error anywhere to explain it.
        run("git", "add", "-f", "Dockerfile")
        for excluded in SPACE_EXCLUDE:
            run("git", "reset", "-q", "--", excluded, check=False)
        run("git", "commit", "-q", "-m", "LedgerLens — deployed tree")

        # Assert the tree before handing it to the Hub. Everything above is a
        # side effect on a temporary branch; a missing file here costs a build,
        # a wait, and a stage the Hub reports as NO_APP_FILE rather than as the
        # missing Dockerfile it is.
        tracked = run("git", "ls-tree", "--name-only", "HEAD").stdout.split()
        if "Dockerfile" not in tracked:
            msg = "the deploy commit has no root Dockerfile; the Space would never build"
            raise RuntimeError(msg)

        return run("git", "push", "--force", "space", f"{temp}:main", check=False)
    finally:
        readme.write_text(original, encoding="utf-8")
        (REPO_ROOT / "Dockerfile").unlink(missing_ok=True)
        run("git", "checkout", "-f", branch, check=False)
        run("git", "branch", "-D", temp, check=False)


def preflight() -> list[str]:
    problems: list[str] = []
    for relative in REQUIRED:
        if not (REPO_ROOT / relative).exists():
            problems.append(f"missing {relative} — check the path")

    config = REPO_ROOT / SPACE_CONFIG
    if config.exists():
        problems.extend(frontmatter_problems(config.read_text(encoding="utf-8")))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", default="ledgerlens", help="Space name (not the full id)")
    parser.add_argument("--public", action="store_true", help="make the Space public")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the preflight only and exit; needs no credentials, so CI can gate it",
    )
    args = parser.parse_args(argv)

    problems = preflight()
    if problems:
        print("preflight failed:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    if args.check:
        print("preflight passed: every file the Space build needs is present")
        return 0

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub is not installed. Run `make setup-api`.")
        return 1

    api = HfApi()
    try:
        user = api.whoami()["name"]
    except Exception:
        print(
            "Not logged in to Hugging Face.\n"
            "  1. Create a WRITE token at https://huggingface.co/settings/tokens\n"
            "  2. apps/api/.venv/bin/hf auth login --token hf_... --add-to-git-credential\n"
        )
        return 1

    repo_id = f"{user}/{args.space}"
    print(f"  user   {user}")
    print(f"  space  {repo_id}  ({'public' if args.public else 'private'})")

    url = api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=not args.public,
        exist_ok=True,
    )
    print(f"  ready  {url}")

    # Applied every deploy, so the Space's configuration is whatever this file
    # says rather than whatever someone last typed into the settings page.
    for key, value in SPACE_VARIABLES.items():
        api.add_space_variable(repo_id, key, value)
    print(f"  config {len(SPACE_VARIABLES)} variables applied from SPACE_VARIABLES")

    # A Space is deployed by pushing a git tree, so the tree has to be committed
    # first. This used to do that for you, which meant an unreviewed edit could
    # reach a deployment and land on `main` under a generic message. A deploy
    # should ship what has been committed and gated; refusing is the safe answer,
    # and naming the files makes it a one-line fix rather than a puzzle.
    dirty = run("git", "status", "--porcelain", check=False).stdout.strip()
    if dirty:
        print("\n  refusing to deploy: the working tree is not clean\n")
        for line in dirty.splitlines():
            print(f"    {line}")
        print("\n  Commit or stash these, let CI go green, then deploy.")
        return 1

    remote_url = f"https://huggingface.co/spaces/{repo_id}"
    existing = run("git", "remote", check=False).stdout.split()
    if "space" in existing:
        run("git", "remote", "set-url", "space", remote_url)
    else:
        run("git", "remote", "add", "space", remote_url)

    branch = run("git", "branch", "--show-current").stdout.strip() or "master"
    print(f"  pushing {branch} -> space/main …")
    try:
        pushed = push_with_space_config(branch)
    except RuntimeError as exc:
        # The temporary branch is already torn down by the `finally` above, so
        # the repository is back where it started and this is safe to re-run.
        print(f"\n  deploy aborted: {exc}")
        return 1
    if pushed.returncode != 0:
        stderr = pushed.stderr.strip()
        print(stderr[-1500:])
        print(f"\n{explain_push_failure(stderr)}")
        return 1

    host = f"https://{user.replace('_', '-')}-{args.space}.hf.space"
    print(f"\n  deployed  {remote_url}")
    print(f"  API       {host}/health")
    print("\n  The first build takes 3-5 minutes. Watch it under the Logs tab.")
    print("\n  Then, under Settings -> Variables and secrets:")
    print("    secrets    DATABASE_URL  ANTHROPIC_API_KEY")
    print("               LANGFUSE_PUBLIC_KEY  LANGFUSE_SECRET_KEY")
    print("    variables  ALLOWED_ORIGINS=<the Vercel origin>   TRUSTED_PROXY_COUNT=1")
    print("\n  Without DATABASE_URL the container boots and reports `degraded`; without")
    print("  ALLOWED_ORIGINS the browser blocks every call and nothing reaches these logs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
