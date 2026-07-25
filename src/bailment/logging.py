"""Structured logging, and the last line of defence against leaking a credential.

Everything bailment logs goes through :func:`redact_secrets`, including log records that
originate in libraries -- uvicorn's access log, SQLAlchemy's echo, httpx -- because those
are where a connection string with an embedded password actually shows up. A redactor
that only covers our own ``log.info`` calls protects the code that was already careful.

The redactor works on two independent signals and needs only one of them to fire:

1. **The key looks sensitive.** ``password``, ``token``, ``secret``, ``key``,
   ``credential``, ``dsn``, ``connection_string``, ``authorization``. Substring matching,
   so ``neon_api_key`` and ``REDIS_TOKEN`` are covered without enumerating them.
2. **The value looks sensitive.** A URI with a password in it, or a string with the shape
   of a well-known credential. This is the case that matters, because the way secrets
   actually escape is ``log.info("provisioned", **provider_response)`` -- a dict whose
   keys nobody chose and nobody reviewed.

Two deliberate asymmetries:

**Over-redaction is the acceptable failure.** A field named ``monkey`` gets caught by the
``key`` substring. That is a mildly annoying log line. The other direction is a
credential in a log aggregator that a dozen people can read and that no rotation can
un-see. The one exception is a short allowlist for names that contain a sensitive
substring but are never sensitive and are needed constantly, ``idempotency_key`` first
among them -- it appears on every request row and an audit trail that cannot show it
cannot answer "did this retry double-provision".

**Bytes are never rendered.** Any ``bytes`` value becomes ``<bytes len=N>`` regardless of
its key, because the only bytes in this system are Fernet ciphertext and raw key
material, and neither has ever helped anybody in a log line.

Exception formatting happens *before* redaction on purpose. A traceback that stringifies
a DSN is exactly as dangerous as a field containing one, and if ``format_exc_info`` ran
after the redactor the traceback would go out untouched.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any, Final, Literal, cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED: Final = "***redacted***"

#: Substrings that make a key sensitive. Lowercased comparison.
SECRET_KEY_HINTS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "key",
        "dsn",
        "connection_string",
        "connectionstring",
        "conn_str",
        "authorization",
        "auth_header",
        "cookie",
        "ciphertext",
        "private",
        "signature",
    }
)

#: Names that contain a hint substring but carry nothing sensitive, and that we need to
#: be able to read. Exact matches only -- an allowlist that does substring matching is
#: not an allowlist, it is a hole.
SAFE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "idempotency_key",
        "key_id",
        "public_key",
        "key_count",
        "output_names",
        "token_count",
    }
)

#: ``scheme://user:password@host`` in any string value. The password is replaced and the
#: rest is kept, because knowing that a worker tried to reach ``db.prod.example.com`` is
#: most of the value of the log line and none of the risk.
_URI_CREDENTIAL_RE: Final = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/?#\[\]@\s]+):(?P<pw>[^/?#\[\]@\s]*)@"
)

#: Shapes of well-known credentials, for the case where a provider hands back a bare
#: token under an innocuous key. Best effort by construction: this list can only ever
#: cover formats somebody thought to add, which is why the key-based rule exists too.
_TOKEN_SHAPE_RE: Final = re.compile(
    r"""(?x)
    \b(?:
        sk-[A-Za-z0-9_\-]{16,}                 # OpenAI-style
      | ghp_[A-Za-z0-9]{20,}                   # GitHub personal access token
      | github_pat_[A-Za-z0-9_]{20,}
      | gho_[A-Za-z0-9]{20,}
      | xox[baprs]-[A-Za-z0-9\-]{10,}          # Slack
      | AKIA[0-9A-Z]{16}                       # AWS access key id
      | AIza[0-9A-Za-z_\-]{20,}                # Google API key
      | gAAAAA[A-Za-z0-9_\-=]{20,}             # Fernet token, i.e. one of ours
      | eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{5,}   # JWT
    )\b
    """
)

#: Nested structures deeper than this are summarised rather than walked. Log payloads
#: that deep are always a mistake, and an unbounded walk is a denial of service waiting
#: for a self-referential provider response.
MAX_DEPTH: Final = 6

_LEVELS: Final[dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

#: Libraries that log request URLs at INFO. A URL can carry a token in its query string,
#: so they start at WARNING and you opt back in when you are debugging them.
_NOISY_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "asyncio", "aiosqlite")

_configured = False


def is_secret_key(name: str) -> bool:
    """Whether a field name is sensitive enough to redact its value outright."""
    lowered = name.lower()
    if lowered in SAFE_KEY_NAMES:
        return False
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def scrub_text(value: str) -> str:
    """Mask credentials embedded in a string, leaving the rest readable.

    Applied to every string that survives the key check, including the event message
    itself, since ``log.error(f"connect failed: {dsn}")`` is a normal thing for a tired
    person to write.
    """
    masked = _URI_CREDENTIAL_RE.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", value
    )
    return _TOKEN_SHAPE_RE.sub(REDACTED, masked)


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: ANN401 - log payloads are Any
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<bytes len={len(bytes(value))}>"
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= MAX_DEPTH:
        return f"<{type(value).__name__} nested too deep>"
    if isinstance(value, Mapping):
        return _scrub_mapping(value, depth + 1)
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, set | frozenset):
        return {_scrub_value(item, depth + 1) for item in value}
    return value


def _scrub_mapping(mapping: Mapping[Any, Any], depth: int) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in mapping.items():
        name = key if isinstance(key, str) else str(key)
        if is_secret_key(name):
            out[key] = f"<bytes len={len(value)}>" if isinstance(value, bytes) else REDACTED
        else:
            out[key] = _scrub_value(value, depth)
    return out


def redact_secrets(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor. Returns a scrubbed copy; never mutates the caller's dict."""
    del logger, method_name
    return cast(EventDict, _scrub_mapping(event_dict, 0))


def _shared_processors() -> list[Processor]:
    """The chain both our own and foreign (stdlib) log records pass through.

    Order matters twice: ``format_exc_info`` must precede :func:`redact_secrets` so that
    tracebacks are scrubbed, and the timestamp is added early so it reflects when the
    event happened rather than when the queue drained.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        redact_secrets,
    ]


def configure_logging(
    level: str = "info",
    fmt: Literal["json", "console"] = "json",
    *,
    force: bool = False,
    stream: Any = None,  # noqa: ANN401 - any writable stream
) -> None:
    """Install the logging configuration for this process.

    Idempotent: the API, the lease engine and the reconciler all call it during startup
    and only the first call does anything, because reconfiguring mid-run detaches
    handlers other threads are already writing to. Pass ``force=True`` from tests.
    """
    global _configured
    if _configured and not force:
        return

    level_no = _LEVELS.get(level.strip().lower(), logging.INFO)
    shared = _shared_processors()
    sink = stream if stream is not None else sys.stdout
    renderer: Processor = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=bool(getattr(sink, "isatty", bool)()))
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain is what pulls library log records through the redactor.
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sink)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_no)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = True
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level_no, logging.WARNING))

    _configured = True


def configure_from_settings(settings: Any, *, force: bool = False) -> None:  # noqa: ANN401
    """Convenience wrapper. Typed loosely so :mod:`bailment.logging` imports nothing."""
    configure_logging(settings.log_level, settings.log_format, force=force)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A bound logger. Call after :func:`configure_logging`, or you get structlog defaults."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def reset_logging() -> None:
    """Forget that configuration happened. Tests only."""
    global _configured
    _configured = False


def bind_request(**values: Any) -> None:  # noqa: ANN401
    """Attach values to every log line emitted by this task until it finishes.

    Used for lease ids and requester principals so that every line about one provisioning
    attempt can be grepped together without threading a logger through six call frames.
    Context is per-asyncio-task, so concurrent leases do not bleed into each other.
    """
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    """Drop everything :func:`bind_request` attached."""
    structlog.contextvars.clear_contextvars()


__all__ = [
    "MAX_DEPTH",
    "REDACTED",
    "SAFE_KEY_NAMES",
    "SECRET_KEY_HINTS",
    "bind_request",
    "clear_request_context",
    "configure_from_settings",
    "configure_logging",
    "get_logger",
    "is_secret_key",
    "redact_secrets",
    "reset_logging",
    "scrub_text",
]
