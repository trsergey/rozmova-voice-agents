"""Session tests over a fake transport, the demo flow and FakeProvider."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from gateway.outbox import Outbox
from gateway.session import Session
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.providers import ProviderError, TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, Raise, Step, text_turn
from rozmova_engine.session import SessionState
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
    ) -> None:
        self.transport = FakeTransport()
        provider = FakeProvider(llm_calls)
        registry = ToolRegistry(subscriber_tools(MockBilling(demo_subscribers())))
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)
        loop = ToolLoop(provider, "gpt-4.1-mini", registry, ctx, self.emit)
        executor = FlowExecutor(flow, loop, registry, ctx, self.emit)
        self.session = Session(
            self.transport,
            executor,
            state,
            debug=debug,
            outbox=outbox,
            turn_timeout=turn_timeout,
        )
        self.task: asyncio.Task[None] | None = None

    async def emit(self, event: Any) -> None:
        await self.session.emit(event)

    async def __aenter__(self) -> "Call":
        self.task = asyncio.create_task(self.session.run())
        await self.transport.until("turn_end")  # the greeting turn
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
