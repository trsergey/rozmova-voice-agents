"""Session tests over a fake transport, the demo flow and FakeProvider."""

import asyncio
import gc
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fakeredis import FakeAsyncRedis
from starlette.websockets import WebSocketDisconnect

from gateway.outbox import Outbox
from gateway.session import Session
from gateway.state_store import SessionStore, session_key
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.providers import ProviderError, TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, Raise, Step, text_turn
from rozmova_engine.session import INTERRUPTED_MARK, SessionState
from rozmova_engine.tool_loop import ROUTE_TOOL, ToolLoop
from rozmova_engine.tools import (
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    ToolContext,
    ToolRegistry,
    demo_subscribers,
    subscriber_tools,
)

DEMO_FLOW = FlowGraph.model_validate_json(
    (
        Path(__file__).resolve().parents[3] / "packages/contracts/tests/fixtures/demo_flow.json"
    ).read_text(encoding="utf-8")
)
DISCONNECT = object()
WAIT = 2.0


class FakeTransport:
    """Frames from the test go to the session; frames from the session are recorded."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None
        self.closed = asyncio.Event()
        self.reading = asyncio.Event()  # cleared = a client that stopped reading
        self.reading.set()
        self._arrived = asyncio.Condition()

    async def receive_text(self) -> str:
        item = await self.incoming.get()
        if item is DISCONNECT:
            raise WebSocketDisconnect(code=1001)
        return str(item)

    async def send_text(self, data: str) -> None:
        await self.reading.wait()
        async with self._arrived:
            self.sent.append(json.loads(data))
            self._arrived.notify_all()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code
        self.closed.set()

    def send(self, frame: object) -> None:
        """A frame (dict → JSON, str as is) or ``DISCONNECT``."""
        if isinstance(frame, dict):
            frame = json.dumps(frame)
        self.incoming.put_nowait(frame)

    async def until(self, event_type: str, count: int = 1) -> list[dict[str, Any]]:
        """Wait until ``count`` events of ``event_type`` were sent; return everything sent."""
        async with asyncio.timeout(WAIT), self._arrived:
            await self._arrived.wait_for(
                lambda: sum(e["type"] == event_type for e in self.sent) >= count
            )
        return self.sent

    def types(self) -> list[str]:
        return [event["type"] for event in self.sent]


def route(name: str) -> list[Step]:
    return [ToolCall(id=f"r-{name}", name=ROUTE_TOOL, arguments={"name": name})]


class Call:
    def __init__(
        self,
        *llm_calls: list[Step],
        flow: FlowGraph = DEMO_FLOW,
        debug: bool = False,
        outbox: Outbox | None = None,
        turn_timeout: float = 30.0,
        redis: FakeAsyncRedis | None = None,
        resume: tuple[SessionStore, SessionState] | None = None,
    ) -> None:
        """``redis`` persists the state; ``resume`` is a claimed store and its state."""
        self.transport = FakeTransport()
        self.provider = provider = FakeProvider(llm_calls)
        registry = ToolRegistry(subscriber_tools(MockBilling(demo_subscribers())))
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        self.resumed = resume is not None
        store: SessionStore | None
        if resume is not None:
            store, state = resume
        else:
            state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)
            store = SessionStore(redis, state.call_id) if redis is not None else None
        loop = ToolLoop(provider, "gpt-4.1-mini", registry, ctx, self.emit)
        executor = FlowExecutor(flow, loop, registry, ctx, self.emit)
        self.session = Session(
            self.transport,
            executor,
            state,
            debug=debug,
            outbox=outbox,
            turn_timeout=turn_timeout,
            store=store,
            resumed=self.resumed,
        )
        self.task: asyncio.Task[None] | None = None

    async def emit(self, event: Any) -> None:
        await self.session.emit(event)

    async def __aenter__(self) -> "Call":
        self.task = asyncio.create_task(self.session.run())
        # A new call starts with the greeting turn, a resumed one with session_started.
        await self.transport.until("session_started" if self.resumed else "turn_end")
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.task is not None
        if not self.task.done():
            self.transport.send(DISCONNECT)
        async with asyncio.timeout(WAIT):
            await self.task

    async def ended(self) -> None:
        assert self.task is not None
        async with asyncio.timeout(WAIT):
            await self.task


async def test_call_starts_with_session_started_greeting_and_turn_end() -> None:
    async with Call() as call:
        sent = call.transport.sent
    assert [e["type"] for e in sent] == ["session_started", "assistant_delta", "turn_end"]
    assert sent[0]["call_id"] == str(call.session.call_id)
    assert sent[1] == {
        "type": "assistant_delta",
        "turn_id": 1,
        "text": "Вітаю! Я асистент Hvylia Mobile. Чим можу допомогти?",
    }
    assert sent[2]["turn_id"] == 1


async def test_turn_streams_text_and_ends_with_timings() -> None:
    llm: list[list[Step]] = [
        [ToolCall(id="c1", name="get_balance")],
        text_turn("На рахунку 120,50 грн."),
    ]
    async with Call(*llm) as call:
        call.transport.send({"type": "user_text", "text": "скільки в мене грошей"})
        sent = await call.transport.until("turn_end", 2)

    turn = sent[3:]
    assert {e["type"] for e in turn} == {"assistant_delta", "turn_end"}
    assert "".join(e["text"] for e in turn if e["type"] == "assistant_delta") == (
        "На рахунку 120,50 грн."
    )
    timings = turn[-1]["timings"]
    assert turn[-1]["turn_id"] == 2
    assert {"llm_ttft", "llm_total", "tool.get_balance"} <= set(timings["stages"])
    assert timings["ttft_ms"] is not None


async def test_debug_session_also_gets_node_and_tool_events() -> None:
    llm: list[list[Step]] = [[ToolCall(id="c1", name="get_balance")], text_turn("120,50 грн.")]
    async with Call(*llm, debug=True) as call:
        call.transport.send({"type": "user_text", "text": "баланс"})
        await call.transport.until("turn_end", 2)
    assert {"node_enter", "tool_call", "tool_result"} <= set(call.transport.types())


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        {"type": "dance"},
        {"type": "user_text", "text": ""},
        {"type": "user_text"},
        {"type": "audio_chunk", "pcm16": "AAAA", "seq": 0},
    ],
)
async def test_invalid_frame_is_an_error_event_and_the_session_goes_on(frame: Any) -> None:
    async with Call(text_turn("Слухаю.")) as call:
        call.transport.send(frame)
        sent = await call.transport.until("error")
        assert sent[-1]["code"] == "invalid_message"

        call.transport.send({"type": "user_text", "text": "алло"})
        await call.transport.until("turn_end", 2)
        assert call.transport.closed_with is None


async def test_hangup_ends_the_session_with_session_end() -> None:
    async with Call() as call:
        call.transport.send({"type": "hangup"})
        await call.ended()
    assert call.transport.types()[-1] == "session_end"
    assert call.transport.sent[-1]["reason"] == "hangup"
    assert call.transport.closed_with == 1000


async def test_flow_end_sends_turn_end_then_session_end_and_closes() -> None:
    async with Call(route("bye")) as call:
        call.transport.send({"type": "user_text", "text": "дякую, до побачення"})
        await call.ended()
    assert call.transport.types()[-3:] == ["assistant_delta", "turn_end", "session_end"]
    assert call.transport.sent[-1]["reason"] == "completed"
    assert call.transport.closed_with == 1000


async def test_handoff_reaches_the_client_before_the_socket_closes() -> None:
    async with Call(route("operator"), text_turn("Хочете новини?"), route("done")) as call:
        call.transport.send({"type": "user_text", "text": "дайте оператора"})
        await call.transport.until("turn_end", 2)
        call.transport.send({"type": "user_text", "text": "ні"})
        await call.ended()
    assert call.transport.types()[-3:] == ["handoff", "turn_end", "session_end"]
    assert call.transport.sent[-1]["reason"] == "handoff"


async def test_client_disconnect_ends_the_session_without_close() -> None:
    async with Call() as call:
        call.transport.send(DISCONNECT)
        await call.ended()
    assert call.transport.closed_with is None


async def test_llm_failure_is_a_typed_error_and_the_session_goes_on() -> None:
    llm: list[list[Step]] = [
        [Raise(ProviderError("503", retryable=True))],
        text_turn("Тепер працює."),
    ]
    async with Call(*llm) as call:
        call.transport.send({"type": "user_text", "text": "баланс"})
        sent = await call.transport.until("error")
        assert sent[-1] == {
            "type": "error",
            "code": "llm_unavailable",
            "message": "The language model is not available.",
            "retryable": True,
        }
        call.transport.send({"type": "user_text", "text": "ще раз"})
        await call.transport.until("turn_end", 2)


async def test_turn_timeout_is_a_typed_error() -> None:
    async with Call([Delay(1.0), TextDelta(text="пізно")], turn_timeout=0.05) as call:
        call.transport.send({"type": "user_text", "text": "алло"})
        sent = await call.transport.until("error")
    assert sent[-1]["code"] == "turn_timeout"
    assert sent[-1]["retryable"] is True


async def test_unexpected_turn_failure_is_an_internal_error() -> None:
    broken = DEMO_FLOW.model_copy(deep=True)
    assist = next(n for n in broken.nodes if n.id == "assist")
    object.__setattr__(assist.data, "tools", ["refund"])  # not in the registry
    async with Call(flow=broken) as call:
        call.transport.send({"type": "user_text", "text": "поверніть гроші"})
        sent = await call.transport.until("error")
        assert sent[-1]["code"] == "internal"
        assert call.transport.closed_with is None


async def test_slow_client_gets_coalesced_deltas() -> None:
    text = "Тариф Оптимум коштує 235 грн на місяць."
    async with Call(text_turn(text, chunk_size=1), outbox=Outbox(maxsize=4)) as call:
        call.transport.reading.clear()
        call.transport.send({"type": "user_text", "text": "мій тариф"})
        await asyncio.sleep(0.05)  # the whole reply is produced while nobody reads
        call.transport.reading.set()
        sent = await call.transport.until("turn_end", 2)

    deltas = [e["text"] for e in sent[3:] if e["type"] == "assistant_delta"]
    assert "".join(deltas) == text
    assert len(deltas) < len(text)


async def test_client_that_stops_reading_is_closed_with_1013() -> None:
    llm = [text_turn("x" * 20, chunk_size=1)]
    outbox = Outbox(maxsize=2, stall_timeout=0.05)
    async with Call(*llm, outbox=outbox) as call:
        call.transport.reading.clear()
        call.transport.send({"type": "user_text", "text": "алло"})
        await call.ended()
    assert call.transport.closed_with == 1013


# --- cancellation (FR-4) -------------------------------------------------------------------


async def test_new_utterance_cancels_the_running_turn() -> None:
    long_reply: list[Step] = [TextDelta(text="Тарифи у нас такі: "), Delay(10)]
    async with Call(long_reply, text_turn("Слухаю вас.")) as call:
        call.transport.send({"type": "user_text", "text": "розкажи про тарифи"})
        await call.transport.until("assistant_delta", 2)
        call.transport.send({"type": "user_text", "text": "стоп, інше питання"})
        sent = await call.transport.until("turn_end", 2)

    types = [e["type"] for e in sent[3:]]
    assert types[:2] == ["assistant_delta", "turn_cancelled"]
    assert sent[4] == {"type": "turn_cancelled", "turn_id": 2}
    assert sent[-1]["turn_id"] == 3

    history = call.session.state.history
    interrupted = [e for e in history if e.interrupted]
    assert [e.message.content for e in interrupted] == ["Тарифи у нас такі: "]
    # The next LLM call saw the cut-off reply with the marker.
    seen = call.provider.calls[-1].messages
    assert any(m.content.endswith(INTERRUPTED_MARK) for m in seen)


async def test_hangup_during_a_turn_closes_without_hanging() -> None:
    async with Call([Delay(10), TextDelta(text="пізно")]) as call:
        call.transport.send({"type": "user_text", "text": "алло"})
        await asyncio.sleep(0.02)
        call.transport.send({"type": "hangup"})
        await call.ended()
    assert call.transport.sent[-1] == {"type": "session_end", "reason": "hangup"}
    assert call.transport.closed_with == 1000


async def test_no_task_exception_is_left_unretrieved() -> None:
    problems: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _, context: problems.append(context))
    try:
        llm: list[list[Step]] = [
            [TextDelta(text="Довга відповідь "), Delay(10)],
            [Raise(ProviderError("503", retryable=False))],
            route("bye"),
        ]
        async with Call(*llm) as call:
            call.transport.send({"type": "user_text", "text": "раз"})
            await call.transport.until("assistant_delta", 2)
            call.transport.send({"type": "user_text", "text": "два"})
            await call.transport.until("error")
            call.transport.send({"type": "user_text", "text": "до побачення"})
            await call.ended()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert problems == []


# --- state in Redis (FR-21) -------------------------------------------------------------


async def saved(redis: FakeAsyncRedis, call: Call) -> SessionState | None:
    return await SessionStore(redis, call.session.call_id).claim()


async def test_state_is_saved_before_each_turn_end() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    llm: list[list[Step]] = [[ToolCall(id="c1", name="get_balance")], text_turn("120,50 грн.")]
    call = Call(*llm, redis=redis)
    seen_at_turn_end: list[int | None] = []
    send_text = call.transport.send_text

    async def record(data: str) -> None:
        if json.loads(data)["type"] == "turn_end":
            stored = await redis.hget(session_key(call.session.call_id), "turn")
            seen_at_turn_end.append(None if stored is None else int(stored))
        await send_text(data)

    call.transport.send_text = record  # type: ignore[method-assign]
    async with call:
        call.transport.send({"type": "user_text", "text": "баланс"})
        await call.transport.until("turn_end", 2)

    assert seen_at_turn_end == [1, 2]
    assert await redis.ttl(session_key(call.session.call_id)) > 0


async def test_disconnect_mid_turn_saves_the_cut_off_reply() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    long_reply: list[Step] = [TextDelta(text="Тарифи у нас такі: "), Delay(10)]
    call = Call(long_reply, redis=redis)
    async with call:
        call.transport.send({"type": "user_text", "text": "розкажи про тарифи"})
        await call.transport.until("assistant_delta", 2)
        call.transport.send(DISCONNECT)
        await call.ended()

    state = await saved(redis, call)
    assert state is not None
    assert state.turn == 2
    assert [(m.role, m.text, m.interrupted) for m in state.transcript()][-2:] == [
        ("user", "розкажи про тарифи", False),
        ("assistant", "Тарифи у нас такі: ", True),
    ]


@pytest.mark.parametrize("frame", [{"type": "hangup"}, {"type": "user_text", "text": "бувай"}])
async def test_finished_call_is_dropped(frame: dict[str, str]) -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    call = Call(route("bye"), redis=redis)
    call_id = call.session.call_id
    async with call:
        assert await redis.exists(session_key(call_id)) == 1
        call.transport.send(frame)
        await call.ended()

    assert await redis.exists(session_key(call_id)) == 0


async def test_resumed_call_replays_history_and_goes_on() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    first = Call(text_turn("Слухаю."), redis=redis)
    async with first:
        first.transport.send({"type": "user_text", "text": "алло"})
        await first.transport.until("turn_end", 2)

    store = SessionStore(redis, first.session.call_id)
    state = await store.claim()
    assert state is not None
    second = Call(text_turn("Так, я тут."), resume=(store, state), debug=True)
    async with second:
        started = second.transport.sent[0]
        second.transport.send({"type": "user_text", "text": "ви тут?"})
        sent = await second.transport.until("turn_end")

    assert started["resumed"] is True
    assert [(m["role"], m["text"]) for m in started["history"]] == [
        ("assistant", "Вітаю! Я асистент Hvylia Mobile. Чим можу допомогти?"),
        ("user", "алло"),
        ("assistant", "Слухаю."),
    ]
    assert sent[1] == {"type": "node_enter", "node_id": state.current_node}
    assert "Так, я тут." in "".join(e["text"] for e in sent if e["type"] == "assistant_delta")
    assert sent[-1]["turn_id"] == 3
    # The model saw the whole conversation, not just the new utterance.
    assert [m.content for m in second.provider.calls[0].messages if m.role == "user"] == [
        "алло",
        "ви тут?",
    ]


async def test_superseded_session_closes_with_4409() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    old = Call(text_turn("Слухаю."), redis=redis)
    async with old:
        new = SessionStore(redis, old.session.call_id)
        assert await new.claim() is not None  # resumed over another connection
        old.transport.send({"type": "user_text", "text": "алло"})
        await old.ended()

    assert old.transport.closed_with == 4409
    assert "turn_end" not in old.transport.types()[3:]


# --- drain for a shutdown (FR-21) -------------------------------------------------------


def soon(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def test_idle_session_ends_with_shutdown_and_keeps_its_state() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    async with Call(redis=redis) as call:
        call.session.drain(soon(5))
        await call.ended()

    assert call.transport.sent[-1] == {"type": "session_end", "reason": "shutdown"}
    assert call.transport.closed_with == 1012
    assert await saved(redis, call) is not None  # the client resumes on the new instance


async def test_drain_lets_the_running_turn_finish() -> None:
    reply: list[Step] = [TextDelta(text="На рахунку "), Delay(0.1), TextDelta(text="120 грн.")]
    async with Call(reply) as call:
        call.transport.send({"type": "user_text", "text": "баланс"})
        await call.transport.until("assistant_delta", 2)
        call.session.drain(soon(5))
        await call.ended()

    assert call.transport.types()[-3:] == ["assistant_delta", "turn_end", "session_end"]
    assert "turn_cancelled" not in call.transport.types()
    assert call.transport.closed_with == 1012


async def test_turn_still_running_at_the_deadline_is_cancelled_and_saved() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    long_reply: list[Step] = [TextDelta(text="Тарифи у нас такі: "), Delay(10)]
    async with Call(long_reply, redis=redis) as call:
        call.transport.send({"type": "user_text", "text": "тарифи"})
        await call.transport.until("assistant_delta", 2)
        call.session.drain(soon(0.05))
        await call.ended()

    assert call.transport.types()[-2:] == ["turn_cancelled", "session_end"]
    state = await saved(redis, call)
    assert state is not None
    assert state.history[-1].interrupted


async def test_no_new_turn_starts_while_draining() -> None:
    reply: list[Step] = [TextDelta(text="Зараз "), Delay(0.1), TextDelta(text="подивлюсь.")]
    async with Call(reply) as call:
        call.transport.send({"type": "user_text", "text": "раз"})
        await call.transport.until("assistant_delta", 2)
        call.session.drain(soon(5))
        call.transport.send({"type": "user_text", "text": "два"})
        await call.ended()

    assert len(call.provider.calls) == 1  # "два" never reached the model
    assert call.transport.sent[-1]["reason"] == "shutdown"


async def test_call_that_ends_during_the_drain_ends_as_usual() -> None:
    async with Call([Delay(0.1), *route("bye")]) as call:
        call.transport.send({"type": "user_text", "text": "до побачення"})
        await asyncio.sleep(0.02)
        call.session.drain(soon(5))
        await call.ended()

    assert call.transport.sent[-1] == {"type": "session_end", "reason": "completed"}
    assert call.transport.closed_with == 1000
