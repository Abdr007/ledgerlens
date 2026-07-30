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

# Files that must be inside the image. If any is missing the Space builds and then
# reports `degraded`, which is a slow way to discover a missing artefact.
SPACE_CONFIG: Final = Path(".hf-space.yml")

REQUIRED: Final = (
    Path("Dockerfile"),
    Path("README.md"),
    SPACE_CONFIG,
    Path("infra/Dockerfile"),
)

# The Hub enforces this in a pre-receive hook, so an over-long description rejects the
# push only after the entire repository has been uploaded, and the message names the
# field without naming the file it lives in. Checking it here costs nothing.
SHORT_DESCRIPTION_MAX: Final = 60


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


#: Never pushed to the Space. Screenshots are PNGs, and the Hub refuses binary files
#: anywhere in a pushed history — not merely in the tip commit.
SPACE_EXCLUDE: Final = ("docs",)


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
        for excluded in SPACE_EXCLUDE:
            run("git", "reset", "-q", "--", excluded, check=False)
        run("git", "commit", "-q", "-m", "LedgerLens — deployed tree")
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
    parser.add_argument("--message", default="Deploy Lexora", help="commit message")
    args = parser.parse_args(argv)

    problems = preflight()
    if problems:
        print("preflight failed:")
        for problem in problems:
            print(f"  {problem}")
        return 1

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

    # Commit whatever is outstanding. A Space is deployed by pushing a git history, so
    # there is nothing to push until the work is committed.
    if run("git", "status", "--porcelain", check=False).stdout.strip():
        run("git", "add", "-A")
        run("git", "commit", "-m", args.message, check=False)
        print("  committed the working tree")

    remote_url = f"https://huggingface.co/spaces/{repo_id}"
    existing = run("git", "remote", check=False).stdout.split()
    if "space" in existing:
        run("git", "remote", "set-url", "space", remote_url)
    else:
        run("git", "remote", "add", "space", remote_url)

    branch = run("git", "branch", "--show-current").stdout.strip() or "master"
    print(f"  pushing {branch} -> space/main …")
    pushed = push_with_space_config(branch)
    if pushed.returncode != 0:
        stderr = pushed.stderr.strip()
        print(stderr[-1500:])
        print(f"\n{explain_push_failure(stderr)}")
        return 1

    print(f"\n  deployed  {remote_url}")
    print(f"  API       https://{user.replace('_', '-')}-{args.space}.hf.space/api/health")
    print("\n  The first build takes 8-12 minutes (it downloads the ONNX models).")
    print("  Watch it under the Logs tab, then add LEXORA_ANTHROPIC_API_KEY under")
    print("  Settings -> Variables and secrets when your key arrives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
