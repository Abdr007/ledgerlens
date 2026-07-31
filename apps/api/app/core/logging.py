"""Structured JSON logging with hard guarantees against secret leakage.

Every log line is a single JSON object. A redacting filter runs on the way out
so that even an accidental `logger.info(api_key)` cannot print a live secret.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

# Patterns that must never reach a log sink.
#
# The labelled-value pattern deliberately consumes to the end of the value rather
# than a single token: `Authorization: Bearer <token>` has the credential in the
# *second* word, so a `\S+` match scrubs the scheme and prints the secret.
# Character class for "the rest of a secret value": everything up to a quote,
# newline, comma or closing bracket. Not itself a credential.
_SECRET_VALUE_TERMINATORS: Final = r'[^"\'\r\n,}\]]+'  # noqa: S105

_REDACTED: Final = "[REDACTED]"

#: `(pattern, replacement)`. Most patterns match the credential alone and are
#: replaced wholesale. The labelled-value pattern cannot: it has to consume the
#: label to know it *is* a secret, so it captures the label and puts it back.
#:
#: Substituting the whole match there scrubbed the key name and the value's
#: opening quote along with the secret, turning `{"api_key":"sk-ant-…"}` into
#: `{[REDACTED]"}` — the secret was gone, but so was the JSON. Every line this
#: module emits is supposed to be one parseable object, and the lines that got
#: mangled were exactly the ones worth reading. Keeping group 1 leaves the
#: closing quote in place, because the terminator class stops before it.
_SECRET_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Labelled values run first, deliberately. A prefix pattern firing first would
    # leave `[REDACTED]` sitting where the value was, which the labelled pattern
    # then matches and wraps again — harmless but ugly (`"[REDACTED]]"`). Taking
    # the whole value in one pass avoids it.
    (
        re.compile(
            r"(?i)([\"']?\b(?:authorization|proxy-authorization|x-api-key|api[-_]?key|"
            r"access[-_]?token|refresh[-_]?token|secret[-_]?key|client[-_]?secret|"
            r"password|passwd|secret|token)\b[\"']?\s*[:=]\s*[\"']?)" + _SECRET_VALUE_TERMINATORS
        ),
        r"\1" + _REDACTED,
    ),
    # Provider-issued keys, by their published prefixes.
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), _REDACTED),
    (re.compile(r"(?:sk|pk)-lf-[A-Za-z0-9_\-]{8,}"), _REDACTED),
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), _REDACTED),
    # Credentials embedded in a connection string.
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s]+:[^@\s]+@"), _REDACTED),
    # A bearer token that appears without its header name.
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/\-]{8,}=*"), r"\1" + _REDACTED),
    # Never let a private key block reach a log, even truncated.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        _REDACTED,
    ),
)

_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a placeholder.

    Structure-preserving: a redacted line is still the JSON object it was, so the
    lines that matter most survive log aggregation.
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class _RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        try:
            serialized = json.dumps(payload, default=str, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            serialized = json.dumps(
                {
                    "ts": payload["ts"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": str(payload["message"]),
                }
            )
        return redact(serialized)


# Attribute names that `logging` sets on a LogRecord itself. Passing any of them
# through `extra=` makes `Logger.makeRecord` raise `KeyError` — at call time, in
# whatever request happens to be running. A log line must never be able to take
# down the work it is describing, so the collision is renamed instead of raised.
_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
)


class _SafeLogger(logging.Logger):
    """A Logger whose `extra=` can never crash the caller.

    A key that collides with a built-in LogRecord attribute is re-emitted under an
    `extra_` prefix rather than discarded, so the diagnostic value survives and the
    record's own fields stay truthful.
    """

    def makeRecord(  # noqa: N802
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, Any] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        if extra:
            collisions = _RESERVED_RECORD_KEYS.intersection(extra)
            if collisions:
                extra = {
                    (f"extra_{key}" if key in collisions else key): value
                    for key, value in extra.items()
                }
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)


# Must run before any module-level `get_logger` call creates a Logger instance.
# Every module reaches its logger through this one, so importing it is enough.
logging.setLoggerClass(_SafeLogger)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger exactly once."""
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_RedactingJsonFormatter())
    root.addHandler(handler)

    # uvicorn installs its own handlers; make them delegate to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
