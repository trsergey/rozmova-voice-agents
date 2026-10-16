import asyncio
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

from rozmova_core.log import REDACTED, configure_logging, log_context
from rozmova_core.settings import Settings


@pytest.fixture
def out() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(Settings(log_json=True, log_level="DEBUG"), stream=stream)
    yield stream
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    logging.getLogger().handlers.clear()


def _records(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line]


def test_records_are_json_with_call_context(out: io.StringIO) -> None:
    log = structlog.get_logger("test")

    with (
        log_context(call_id="c-1", bot_id="b-1", bot_version=3, user_id="u-1"),
        log_context(turn_id="t-7"),
    ):
        log.info("turn_started", node="greeting")
    log.info("after")

    inside, after = _records(out)
    assert inside["event"] == "turn_started"
    assert inside["level"] == "info"
    assert inside["logger"] == "test"
    assert "timestamp" in inside
    assert inside["node"] == "greeting"
    assert {k: inside[k] for k in ("call_id", "bot_id", "bot_version", "turn_id", "user_id")} == {
        "call_id": "c-1",
        "user_id": "u-1",
        "bot_id": "b-1",
        "bot_version": 3,
        "turn_id": "t-7",
    }
    assert "call_id" not in after
    assert "turn_id" not in after


async def test_context_is_isolated_between_tasks(out: io.StringIO) -> None:
    log = structlog.get_logger("test")

    async def turn(turn_id: str) -> None:
        with log_context(turn_id=turn_id):
            await asyncio.sleep(0)
            log.info("turn", expected=turn_id)

    with log_context(call_id="c-1"):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(turn("t-1"))
            tg.create_task(turn("t-2"))

    records = [r for r in _records(out) if r["logger"] == "test"]
    assert len(records) == 2
    for record in records:
        assert record["call_id"] == "c-1"
        assert record["turn_id"] == record["expected"]


def test_trace_id_comes_from_active_span(out: io.StringIO) -> None:
    tracer = TracerProvider().get_tracer("test")
    log = structlog.get_logger("test")

    log.info("outside")
    with tracer.start_as_current_span("turn") as span:
        log.info("inside")

    outside, inside = _records(out)
    assert "trace_id" not in outside
    assert inside["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert inside["span_id"] == format(span.get_span_context().span_id, "016x")


def test_secrets_are_redacted(out: io.StringIO) -> None:
    structlog.get_logger("test").info(
        "provider_configured",
        api_key="sk-live-123",
        ticket_secret=SecretStr("hmac"),
        password="hunter2",  # noqa: S106
        tokens_in=42,
        headers={"Authorization": "Bearer abc", "accept": "json"},
        dsn="postgresql+asyncpg://rozmova:dbpass@db:5432/rozmova",
        wrapped=SecretStr("plain"),
    )

    (record,) = _records(out)
    assert record["api_key"] == REDACTED
    assert record["ticket_secret"] == REDACTED
    assert record["password"] == REDACTED
    assert record["wrapped"] == REDACTED
    assert record["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert record["tokens_in"] == 42
    assert record["dsn"] == f"postgresql+asyncpg://{REDACTED}@db:5432/rozmova"
    for secret in ("sk-live-123", "hmac", "hunter2", "Bearer abc", "dbpass", "plain"):
        assert secret not in out.getvalue()


def test_secret_query_parameters_are_redacted(out: io.StringIO) -> None:
    """uvicorn logs every WebSocket with its full URL; the ticket must not reach the log."""
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]',
        "100.64.0.4:16038",
        "/ws/calls/b-1?call_id=c-1&ticket=eyJpZCI6IjQw.sig-123",
    )
    structlog.get_logger("test").info(
        "fetched",
        url="https://api.example/v1?access_token=tok-1&page=2&api_key=k-1#top",
        note="keep ?monkey=1 and ?tokens=5",
    )

    uvicorn_line, fetched = _records(out)
    assert uvicorn_line["event"] == (
        f'100.64.0.4:16038 - "WebSocket /ws/calls/b-1?call_id=c-1&ticket={REDACTED}" [accepted]'
    )
    assert fetched["url"] == (
        f"https://api.example/v1?access_token={REDACTED}&page=2&api_key={REDACTED}#top"
    )
    assert fetched["note"] == "keep ?monkey=1 and ?tokens=5"
    for secret in ("eyJpZCI6IjQw", "sig-123", "tok-1", "k-1"):
        assert secret not in out.getvalue()


def test_stdlib_loggers_share_the_pipeline(out: io.StringIO) -> None:
    with log_context(call_id="c-9"):
        logging.getLogger("uvicorn.error").warning("token=%s", "x")

    (record,) = _records(out)
    assert record["logger"] == "uvicorn.error"
    assert record["level"] == "warning"
    assert record["call_id"] == "c-9"


def test_level_filters_records(out: io.StringIO) -> None:
    configure_logging(Settings(log_level="WARNING"), stream=out)
    structlog.get_logger("test").info("hidden")
    structlog.get_logger("test").warning("shown")

    assert [r["event"] for r in _records(out)] == ["shown"]
