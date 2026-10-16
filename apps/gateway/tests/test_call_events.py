"""``CallEventWriter`` over fake Redis and ``CallRecorder`` on protocol events (ROZ-52)."""

import asyncio
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis
from opentelemetry.sdk.trace import TracerProvider
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.call_events import CallEventWriter, CallRecorder
from rozmova_contracts import protocol
from rozmova_core.call_events import MAXLEN, STREAM, CallEvent

CALL_ID = UUID("3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c")
BOT_ID = UUID("00000000-0000-0000-0000-00000000d3e0")


async def entries(redis: FakeAsyncRedis) -> list[dict[str, Any]]:
    out = []
    raw: Any = await redis.xrange(STREAM)
    for _, fields in raw:
        out.append({**fields, "payload": json.loads(fields["payload"])})
    return out


def event(type_: str = "node_enter", **payload: Any) -> CallEvent:
    return CallEvent(call_id=CALL_ID, bot_id=BOT_ID, type=type_, payload=payload)


async def test_writer_adds_entries_with_the_call_fields() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    writer = CallEventWriter(redis)
    writer.start()

    writer.record(event("node_enter", node_id="assist"))
    writer.record(event("user_message", text="скільки в мене грошей"))
    await writer.aclose()

    first, second = await entries(redis)
    assert first["call_id"] == str(CALL_ID)
    assert first["bot_id"] == str(BOT_ID)
    assert first["type"] == "node_enter"
    assert first["payload"] == {"node_id": "assist"}
    assert first["ts"].endswith("+00:00")
    assert "trace_id" not in first  # no span is recording
    assert second["payload"] == {"text": "скільки в мене грошей"}


async def test_trace_context_travels_with_the_event() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    writer = CallEventWriter(redis)
    writer.start()
    recorder = CallRecorder(writer, call_id=CALL_ID, bot_id=BOT_ID, user_id="code:c-1", debug=False)

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("turn") as span:
        recorder.user_message("привіт")
    await writer.aclose()

    (entry,) = await entries(redis)
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert entry["span_id"] == format(span.get_span_context().span_id, "016x")


class HangingPipeline:
    """A pipeline whose ``execute`` never returns: Redis that stopped answering."""

    def __init__(self) -> None:
        self.added = 0

    async def __aenter__(self) -> "HangingPipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def xadd(self, *args: object, **kwargs: object) -> None:
        self.added += 1

    async def execute(self) -> None:
        await asyncio.Event().wait()


class HangingRedis:
    def __init__(self) -> None:
        self.pipe = HangingPipeline()

    def pipeline(self, transaction: bool = True) -> HangingPipeline:
        return self.pipe


async def test_the_stream_is_capped_at_about_100k(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    calls: list[dict[str, object]] = []
    pipe_cls = type(redis.pipeline())
    real_xadd = pipe_cls.xadd

    def spy(self: object, name: str, fields: object, **kwargs: object) -> object:
        calls.append({"name": name, **kwargs})
        return real_xadd(self, name, fields, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(pipe_cls, "xadd", spy)
    writer = CallEventWriter(redis)
    writer.start()
    writer.record(event())
    await writer.aclose()

    assert MAXLEN == 100_000
    assert calls == [{"name": STREAM, "maxlen": MAXLEN, "approximate": True}]


async def test_record_never_waits_and_a_full_queue_drops() -> None:
    writer = CallEventWriter(HangingRedis(), queue_size=10)  # type: ignore[arg-type]
    writer.start()
    await asyncio.sleep(0)  # the writer takes the first batch and hangs on it

    started = time.perf_counter()
    for i in range(1_000):
        writer.record(event(n=i))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5  # no await, no Redis round trip on the response path
    assert writer.dropped > 0
    started = time.perf_counter()
    await writer.aclose(flush_seconds=0.2)
    assert time.perf_counter() - started < 1.0  # shutdown is bounded too


async def test_a_failed_batch_is_dropped_and_the_writer_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    writer = CallEventWriter(redis)
    real_send = writer._send
    failures = [RedisConnectionError("down")]

    async def flaky_send(batch: list[CallEvent]) -> None:
        if failures:
            raise failures.pop()
        await real_send(batch)

    monkeypatch.setattr(writer, "_send", flaky_send)
    writer.start()
    writer.record(event(n=1))
    await asyncio.sleep(0.05)
    writer.record(event(n=2))
    await writer.aclose()

    assert writer.dropped == 1
    assert [e["payload"] for e in await entries(redis)] == [{"n": 2}]


class Collector(CallEventWriter):
    """Keeps the recorded events instead of sending them."""

    def __init__(self) -> None:
        super().__init__(FakeAsyncRedis())
        self.events: list[CallEvent] = []

    def record(self, event: CallEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def recorder(collector: Collector, *, debug: bool = False) -> CallRecorder:
    return CallRecorder(collector, call_id=CALL_ID, bot_id=BOT_ID, user_id="code:c-1", debug=debug)


def test_streamed_deltas_are_one_assistant_message_before_turn_end() -> None:
    collector = Collector()
    rec = recorder(collector)
    rec.observe(protocol.AssistantDelta(turn_id=2, text="На рахунку "))
    rec.observe(protocol.AssistantDelta(turn_id=2, text="120,50 грн."))
    rec.observe(
        protocol.TurnEnd(
            turn_id=2, timings=protocol.TurnTimings(total_ms=10, ttft_ms=4, stages={}), sources=[]
        )
    )

    assert collector.types() == ["assistant_message", "turn_end"]
    message, turn_end = collector.events
    assert message.payload == {"turn_id": 2, "text": "На рахунку 120,50 грн.", "interrupted": False}
    assert turn_end.payload["timings"]["total_ms"] == 10


def test_a_cancelled_turn_keeps_its_partial_reply_as_interrupted() -> None:
    collector = Collector()
    rec = recorder(collector)
    rec.observe(protocol.AssistantDelta(turn_id=3, text="Тариф Безлім"))
    rec.observe(protocol.TurnCancelled(turn_id=3))
    rec.observe(protocol.TurnCancelled(turn_id=4))  # nothing streamed: no message

    assert collector.types() == ["assistant_message", "turn_cancelled", "turn_cancelled"]
    assert collector.events[0].payload == {
        "turn_id": 3,
        "text": "Тариф Безлім",
        "interrupted": True,
    }


def test_session_started_names_the_caller_without_the_history() -> None:
    collector = Collector()
    rec = recorder(collector, debug=True)
    rec.observe(
        protocol.SessionStarted(
            call_id=CALL_ID,
            bot_version=3,
            resumed=True,
            history=[protocol.HistoryMessage(role="user", text="привіт", interrupted=False)],
        )
    )

    (started,) = collector.events
    assert started.type == "session_started"
    assert started.payload == {
        "call_id": str(CALL_ID),
        "bot_version": 3,
        "resumed": True,
        "user_id": "code:c-1",
        "debug": True,
    }


def test_closing_is_recorded_with_the_code() -> None:
    collector = Collector()
    rec = recorder(collector)
    rec.closed(1012, resumable=True)
    rec.closed(None, resumable=True)

    assert [e.payload for e in collector.events] == [
        {"code": 1012, "resumable": True},
        {"code": None, "resumable": True},
    ]


def test_every_event_carries_its_call() -> None:
    collector = Collector()
    other = uuid4()
    CallRecorder(collector, call_id=other, bot_id=BOT_ID, user_id="user:u-1", debug=False).observe(
        protocol.NodeEnter(node_id="assist")
    )

    (entry,) = collector.events
    assert entry.call_id == other
    assert entry.fields()["payload"] == '{"node_id":"assist"}'
