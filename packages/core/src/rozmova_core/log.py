"""structlog JSON logging with call context and secret redaction.

Every record carries whatever of ``call_id``, ``bot_id``, ``bot_version`` and ``turn_id`` is
bound in the current task (``log_context``), plus ``trace_id``/``span_id`` of the active
OpenTelemetry span. Standard-library loggers (uvicorn, SQLAlchemy) go through the same
processors, so the output is one JSON stream.
"""

import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from opentelemetry import trace
from pydantic import SecretStr
from structlog.types import EventDict, Processor, WrappedLogger

from rozmova_core.settings import Settings

REDACTED = "***"

# Matches key names such as `api_key`, `ticket_secret`, `access_token`, `password`, but not
# counters like `tokens_in`.
_SECRET_KEY = re.compile(
    r"(^|_)(key|secret|password|passwd|token|authorization|cookie)$", re.IGNORECASE
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def _redact(key: str, value: Any) -> Any:
    if isinstance(value, SecretStr) or (_SECRET_KEY.search(key) and value is not None):
        return REDACTED
    if isinstance(value, str):
        return _URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", value)
    if isinstance(value, Mapping):
        return {k: _redact(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(key, v) for v in value]
    return value


def redact_secrets(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Mask values under secret-looking keys, ``SecretStr`` values and URL credentials."""
    return {k: _redact(k, v) for k, v in event_dict.items()}


def add_trace_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Add ``trace_id``/``span_id`` of the current span, if one is recording."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Configure structlog and route stdlib logging through it. Call once at startup.

    Records go to ``stream``, stdout by default.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_secrets,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)


@contextmanager
def log_context(
    *,
    call_id: str | None = None,
    bot_id: str | None = None,
    bot_version: int | None = None,
    turn_id: str | None = None,
) -> Iterator[None]:
    """Bind call identifiers for log records emitted in the current task.

    Context variables are copied into child tasks, so a turn task inherits the call's
    ``call_id`` and binds its own ``turn_id`` without affecting siblings.
    """
    values = {
        "call_id": call_id,
        "bot_id": bot_id,
        "bot_version": bot_version,
        "turn_id": turn_id,
    }
    with structlog.contextvars.bound_contextvars(
        **{k: v for k, v in values.items() if v is not None}
    ):
        yield
