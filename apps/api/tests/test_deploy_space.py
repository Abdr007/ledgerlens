"""The Hugging Face Spaces deploy contract.

`scripts/deploy_space.py` had two defects on the day it was first run, and both were
the kind a test catches in milliseconds and a deploy catches in minutes:

* its preflight required a `Dockerfile` at the repository root — true of the project it
  was ported from, false here — so it aborted before reaching the Hub, every time;
* `git add -A` honoured `.gitignore`, which lists `/Dockerfile` precisely *because* the
  root copy is generated, so the deploy commit went up without the one file the Space
  build reads. The Hub reported `NO_APP_FILE` and named nothing.

The frontmatter rules are enforced by the Hub in a pre-receive hook, so a violation
rejects the push only after the entire repository has been uploaded, and the error names
the offending field without naming the file it lives in. These tests move all of it to
`make test`.

`scripts/` sits at the repository root, outside the `apps/api` testpath, so the module is
loaded by path rather than imported as a package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_deploy_space() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "deploy_space", REPO_ROOT / "scripts" / "deploy_space.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deploy_space = _load_deploy_space()


class TestPreflight:
    """The regression that stopped this script from ever running."""

    def test_the_committed_repository_passes_preflight(self) -> None:
        assert deploy_space.preflight() == []

    def test_a_root_dockerfile_is_not_required(self) -> None:
        """It is generated onto the deploy commit, so requiring it aborts every run.

        The ported original listed `Dockerfile` in `REQUIRED`, which is correct for a
        project that tracks one at the root and wrong for this one. Nothing else in the
        repository could have contradicted it — the file legitimately does not exist.
        """
        assert Path("Dockerfile") not in deploy_space.REQUIRED
        assert not (REPO_ROOT / "Dockerfile").exists(), (
            "a root Dockerfile is present; it should only exist during a deploy"
        )

    def test_the_real_dockerfile_is_required(self) -> None:
        assert Path("infra/Dockerfile") in deploy_space.REQUIRED
        assert (REPO_ROOT / "infra" / "Dockerfile").exists()

    def test_a_missing_required_file_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deploy_space, "REQUIRED", (Path("infra/nope.txt"),))
        problems = deploy_space.preflight()
        assert len(problems) == 1
        assert "infra/nope.txt" in problems[0]


class TestTheGeneratedRootDockerfile:
    """The `NO_APP_FILE` defect, pinned from both ends."""

    def test_the_root_dockerfile_is_gitignored(self) -> None:
        """This is the precondition that makes `git add -A` insufficient.

        If this ever stops being true the force-add below becomes unnecessary — but
        silently, and in the safe direction. If it stays true and the force-add is
        removed, the Space builds nothing.
        """
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/Dockerfile" in gitignore.splitlines()

    def test_the_deploy_commit_force_adds_it(self) -> None:
        """A plain `git add -A` drops an ignored path without complaining."""
        source = (REPO_ROOT / "scripts" / "deploy_space.py").read_text(encoding="utf-8")
        assert '"git", "add", "-f", "Dockerfile"' in source, (
            "the generated root Dockerfile must be force-added; .gitignore ignores it"
        )

    def test_the_commit_is_inspected_before_it_is_pushed(self) -> None:
        """Belt and braces: the tree is checked, not assumed, before the push."""
        source = (REPO_ROOT / "scripts" / "deploy_space.py").read_text(encoding="utf-8")
        assert '"git", "ls-tree"' in source
        assert "the Space would never build" in source

    def test_the_dockerfile_copied_is_the_one_that_is_gated(self) -> None:
        """CI builds `infra/Dockerfile`; the Space builds whatever this copies."""
        source = (REPO_ROOT / "scripts" / "deploy_space.py").read_text(encoding="utf-8")
        assert '"infra" / "Dockerfile"' in source
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "-f infra/Dockerfile" in workflow


class TestSpaceExclusions:
    def test_binary_directories_are_kept_off_the_space(self) -> None:
        """The Hub polices binary files across a pushed history, not just the tip."""
        assert "docs" in deploy_space.SPACE_EXCLUDE
        assert "eval/testset" in deploy_space.SPACE_EXCLUDE

    def test_every_exclusion_is_also_out_of_the_build_context(self) -> None:
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        for excluded in deploy_space.SPACE_EXCLUDE:
            assert excluded in dockerignore, f"{excluded} reaches the image but not the Space"


class TestSpaceVariables:
    def test_no_secret_is_declared_in_a_public_file(self) -> None:
        """`SPACE_VARIABLES` is committed. Secrets are set by hand, once, in the UI."""
        forbidden = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")
        leaked = [
            name
            for name in deploy_space.SPACE_VARIABLES
            if any(marker in name.upper() for marker in forbidden)
        ]
        assert not leaked, f"secret-shaped names in a public file: {leaked}"

    def test_cors_names_an_origin_rather_than_a_wildcard(self) -> None:
        origins = deploy_space.SPACE_VARIABLES["ALLOWED_ORIGINS"]
        assert "*" not in origins
        assert origins.startswith("https://")


class TestShortDescription:
    def test_committed_space_config_is_deployable(self) -> None:
        """The regression guard. This is what gets prepended to the pushed README."""
        config = (REPO_ROOT / ".hf-space.yml").read_text(encoding="utf-8")
        assert deploy_space.frontmatter_problems(config) == []

    def test_the_readme_carries_no_front_matter(self) -> None:
        """It lives in .hf-space.yml so GitHub does not render it as a table."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert not readme.startswith("---")

    def test_over_long_description_is_rejected(self) -> None:
        too_long = "x" * (deploy_space.SHORT_DESCRIPTION_MAX + 1)
        problems = deploy_space.frontmatter_problems(
            f"sdk: docker\nshort_description: {too_long}\n"
        )
        assert len(problems) == 1
        # The count has to be in the message; "too long" without a number means opening
        # the file and counting by hand.
        assert str(deploy_space.SHORT_DESCRIPTION_MAX + 1) in problems[0]

    def test_exactly_at_the_limit_is_accepted(self) -> None:
        at_limit = "x" * deploy_space.SHORT_DESCRIPTION_MAX
        assert (
            deploy_space.frontmatter_problems(f"sdk: docker\nshort_description: {at_limit}\n") == []
        )

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_quotes_are_stripped_before_measuring(self, quote: str) -> None:
        """The live description is quoted, because it contains an em dash and a comma."""
        described = "Invoices in, verified data out — or a human review queue"
        assert len(described) <= deploy_space.SHORT_DESCRIPTION_MAX
        problems = deploy_space.frontmatter_problems(
            f"sdk: docker\nshort_description: {quote}{described}{quote}\n"
        )
        assert problems == []

    def test_colon_inside_a_description_is_measured_whole(self) -> None:
        """Splitting on every colon would measure only the text before it.

        Constructed so the head is comfortably short while the whole description is over
        the limit: a parser that truncated at the inner colon would report no problem
        here, and the Hub would reject the push anyway. That is the precise failure this
        check exists to prevent, so the case has to be able to fail.
        """
        head = "LedgerLens"
        described = f"{head}: {'y' * deploy_space.SHORT_DESCRIPTION_MAX}"
        assert len(head) < deploy_space.SHORT_DESCRIPTION_MAX < len(described)

        problems = deploy_space.frontmatter_problems(
            f'sdk: docker\nshort_description: "{described}"\n'
        )
        assert len(problems) == 1
        assert str(len(described)) in problems[0]


class TestSdkDeclaration:
    def test_missing_docker_sdk_is_rejected(self) -> None:
        problems = deploy_space.frontmatter_problems("title: LedgerLens\napp_port: 7860\n")
        assert any("sdk: docker" in problem for problem in problems)

    def test_the_committed_port_is_the_one_the_image_listens_on(self) -> None:
        config = (REPO_ROOT / ".hf-space.yml").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
        assert "app_port: 7860" in config
        assert "PORT=7860" in dockerfile


class TestPushFailureDiagnosis:
    """Each branch names the one thing that acts on it, or says it does not know."""

    def test_metadata_rejection_points_at_the_readme(self) -> None:
        stderr = (
            "remote: Sorry, your push was rejected during YAML metadata verification:\n"
            'remote: - Error: "short_description" length must be less than or equal to 60'
        )
        assert "README.md" in deploy_space.explain_push_failure(stderr)

    def test_binary_rejection_does_not_blame_the_readme(self) -> None:
        stderr = (
            "remote: Your push was rejected because it contains binary files.\n"
            "remote: Please use https://huggingface.co/docs/hub/xet to store binary files.\n"
            "remote: Offending files:\n"
            "remote:   - docs/screens/dashboard.png (ref: refs/heads/main)\n"
            "! [remote rejected] main -> main (pre-receive hook declined)"
        )
        message = deploy_space.explain_push_failure(stderr)
        assert "binary" in message
        assert "README" not in message
        assert "token" not in message

    def test_auth_rejection_points_at_the_token(self) -> None:
        stderr = "remote: Authentication failed\nfatal: could not read Password"
        assert "token" in deploy_space.explain_push_failure(stderr)

    def test_unrecognised_rejection_invents_nothing(self) -> None:
        """Silence about the cause beats a confident wrong cause."""
        message = deploy_space.explain_push_failure("remote: something entirely new")
        assert "reason is above" in message
        assert "README" not in message
        assert "token" not in message
