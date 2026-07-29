"""Timeout + exponential-backoff retry for every outbound call.

Spec §7: *"Every external call (Claude, DB) wrapped with timeout +
exponential-backoff retry (3 attempts)."*

Deliberate design choices:

* **Only transient failures are retried.** A 400 from Claude or a unique-constraint
  violation is deterministic — retrying it burns budget and hides the bug.
* **Full jitter.** Backoff is `uniform(0, base * 2**n)`, which decorrelates retries
  from concurrent callers far better than fixed exponential sleeps.
* **The timeout is per attempt**, so a wedged connection cannot consume the whole
  budget; the caller-visible ceiling is `attempts * timeout + backoff`.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_BACKOFF_S = 8.0


def _full_jitter(base_delay: float, attempt: int) -> float:
    """Return a delay uniformly drawn from `[0, base * 2**attempt]`, capped."""
    ceiling: float = min(float(base_delay) * float(2**attempt), _MAX_BACKOFF_S)
    # `secrets` avoids a cryptographically-weak PRNG lint without changing behaviour.
    fraction: float = secrets.randbelow(10_000) / 10_000
    return ceiling * fraction


@dataclass(slots=True)
class RetryPolicy:
    """How hard to try, and what counts as worth retrying."""

    attempts: int = 3
    base_delay_s: float = 0.5
    timeout_s: float = 90.0
    retry_on: tuple[type[BaseException], ...] = field(
        default_factory=lambda: (TimeoutError, ConnectionError, OSError)
    )


@dataclass(slots=True)
class RetryOutcome:
    """Observability record for a completed retry loop."""

    attempts_used: int
    elapsed_ms: int


class RetriesExhaustedError(Exception):
    """Raised when every attempt failed. Carries the final underlying cause."""

    def __init__(self, operation: str, attempts: int, cause: BaseException) -> None:
        super().__init__(f"{operation} failed after {attempts} attempt(s): {cause}")
        self.operation = operation
        self.attempts = attempts
        self.cause = cause


async def run_with_retry[T](
    operation: str,
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
) -> tuple[T, RetryOutcome]:
    """Run `fn` under `policy`, returning its result plus a retry record.

    Raises:
        RetriesExhaustedError: every attempt failed with a retryable error.
        Exception: the original error, unchanged, when it is *not* retryable.
    """
    if policy.attempts < 1:
        msg = "RetryPolicy.attempts must be >= 1"
        raise ValueError(msg)

    started = time.perf_counter()
    last_error: BaseException | None = None

    for attempt in range(policy.attempts):
        try:
            result = await asyncio.wait_for(fn(), timeout=policy.timeout_s)
        except asyncio.CancelledError:
            # Cooperative cancellation is not a failure — never swallow it.
            raise
        except policy.retry_on as exc:
            last_error = exc
            is_final = attempt == policy.attempts - 1
            logger.warning(
                "retry_attempt_failed",
                extra={
                    "operation": operation,
                    "attempt": attempt + 1,
                    "attempts_total": policy.attempts,
                    "error_type": type(exc).__name__,
                    "will_retry": not is_final,
                },
            )
            if is_final:
                break
            await asyncio.sleep(_full_jitter(policy.base_delay_s, attempt))
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return result, RetryOutcome(attempts_used=attempt + 1, elapsed_ms=elapsed_ms)

    assert last_error is not None  # loop only exits here after a retryable failure
    raise RetriesExhaustedError(operation, policy.attempts, last_error) from last_error
