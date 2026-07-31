"""`/health` must report the tracer that exists, not the one that was configured.

Langfuse ran disabled in production for days behind an `"enabled"` health field,
because the field was derived from `settings.langfuse_enabled` — "both keys are
present" — while `get_tracer()` had quietly fallen back to the local sink. The
three states below are exactly the ones that were previously collapsed into two.
"""

from __future__ import annotations

from app.core.settings import Settings
from app.core.tracing import LangfuseTracer, LocalTracer, get_tracer
from app.routers.health import langfuse_state


def test_built_exporter_reads_enabled() -> None:
    assert langfuse_state(tracer_mode="langfuse", configured=True) == "enabled"


def test_no_keys_reads_disabled() -> None:
    assert langfuse_state(tracer_mode="local", configured=False) == "disabled"


def test_configured_but_fell_back_reads_unavailable() -> None:
    """The state the old field could not express — and the one that mattered."""
    assert langfuse_state(tracer_mode="local", configured=True) == "unavailable"


def test_local_tracer_reports_its_own_mode() -> None:
    assert LocalTracer().mode == "local"


def test_every_sink_declares_a_mode() -> None:
    """Both sinks carry `mode`, and the process tracer reports one of them.

    `Tracer` is not `@runtime_checkable`, so this asserts on the classes rather
    than through `isinstance` — and mypy is the real gate here: `mode` is on the
    Protocol, so a sink that omits it fails the type check before any test runs.
    """
    assert isinstance(LangfuseTracer.mode, property)
    assert get_tracer().mode in {"local", "langfuse"}


def test_test_environment_has_no_keys_so_health_says_disabled() -> None:
    # conftest blanks both keys precisely so the suite never exports spans.
    settings = Settings()
    assert settings.langfuse_enabled is False
    assert langfuse_state(tracer_mode=get_tracer().mode, configured=False) == "disabled"
