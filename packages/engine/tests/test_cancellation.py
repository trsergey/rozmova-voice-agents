"""What the engine leaves behind when a turn is cancelled (FR-4, SDD "Діалоговий гейтвей")."""

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from rozmova_contracts import protocol
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.providers import Message, TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, Step, text_turn
from rozmova_engine.session import INTERRUPTED_MARK, HistoryEntry, PendingAction, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import Tool, ToolContext, ToolRegistry, ToolResult
from rozmova_engine.tools.shield import complete_despite_cancel

CTX = ToolContext(subscriber_id="demo")


# --- complete_despite_cancel -------------------------------------------------------------


async def test_shielded_work_finishes_although_the_caller_is_cancelled() -> None:
    finished = asyncio.Event()

    async def activate() -> str:
        await asyncio.sleep(0.05)
        finished.set()
        return "activated"

    caller = asyncio.create_task(complete_despite_cancel(activate()))
    await asyncio.sleep(0.01)
    caller.cancel()

    assert await caller == ("activated", True)
    assert finished.is_set()


async def test_without_cancel_the_flag_is_false() -> None:
    async def quick() -> int:
        return 7

    assert await complete_despite_cancel(quick()) == (7, False)


async def test_cancelled_work_still_raises() -> None:
    async def stuck() -> None:
        await asyncio.sleep(10)

    work = asyncio.ensure_future(stuck())
    caller = asyncio.create_task(complete_despite_cancel(work))
    await asyncio.sleep(0.01)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


# --- the tool loop -------------------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class Harness:
    def __init__(self, *turns: list[Step], tools: Sequence[Tool[Any]] = ()) -> None:
        self.provider = FakeProvider(turns)
        self.registry = ToolRegistry(tools)
        self.state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1, turn=2)
        self.state.history.append(
            HistoryEntry(turn=2, message=Message(role="user", content="розкажи про тарифи"))
        )
        self.events: list[protocol.ProtocolMessage] = []
        self.loop = ToolLoop(self.provider, "m", self.registry, CTX, self.emit)

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        self.events.append(event)

    async def run_and_cancel(self, after: float = 0.02) -> None:
        tools = sorted(self.registry.names)
        task = asyncio.create_task(
            self.loop.run(self.state, TurnTimer(), system=["prompt"], tools=tools)
        )
        await asyncio.sleep(after)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def tool_results(self) -> list[Any]:
        return [
            json.loads(e.message.content) for e in self.state.history if e.message.role == "tool"
        ]


async def test_partial_reply_is_kept_as_interrupted() -> None:
    h = Harness([TextDelta(text="У нас є тарифи "), TextDelta(text="Старт, "), Delay(10)])
    await h.run_and_cancel()

    last = h.state.history[-1]
    assert last.interrupted
    assert last.message.content == "У нас є тарифи Старт, "
    assert h.state.llm_messages()[-1].content.endswith(INTERRUPTED_MARK)


async def test_nothing_is_recorded_when_cancelled_before_any_text() -> None:
    h = Harness([Delay(10), TextDelta(text="пізно")])
    await h.run_and_cancel()
    assert [e.message.role for e in h.state.history] == ["user"]


async def test_side_effect_tool_finishes_and_its_result_is_recorded() -> None:
    activated: list[str] = []

    async def activate(_: NoArgs, __: ToolContext) -> ToolResult:
        await asyncio.sleep(0.05)
        activated.append("roaming_eu")
        return {"status": "activated"}

    h = Harness(
        [ToolCall(id="a", name="activate")],
        text_turn("не дійде"),
        tools=[Tool("activate", "", NoArgs, activate, side_effect=True)],
    )
    await h.run_and_cancel(after=0.01)

    assert activated == ["roaming_eu"]
    assert h.tool_results() == [{"status": "activated"}]


async def test_calls_that_did_not_run_get_a_cancelled_result() -> None:
    async def slow(_: NoArgs, __: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return {}

    h = Harness(
        [ToolCall(id="s1", name="slow"), ToolCall(id="s2", name="slow")],
        tools=[Tool("slow", "", NoArgs, slow)],
    )
    await h.run_and_cancel()

    assert [r["error"] for r in h.tool_results()] == ["cancelled", "cancelled"]
    # Every tool call has its result, so the next LLM request is valid.
    calls = h.state.history[1].message.tool_calls
    results = [e.message.tool_call_id for e in h.state.history if e.message.role == "tool"]
    assert [c.id for c in calls] == results


async def test_tool_timeout_also_reaches_the_client_as_a_typed_error() -> None:
    async def hangs(_: NoArgs, __: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return {}

    h = Harness([ToolCall(id="h", name="hangs")], text_turn("Не вдалося перевірити."))
    h.registry = ToolRegistry([Tool("hangs", "", NoArgs, hangs)], timeout=0.01)
    h.loop = ToolLoop(h.provider, "m", h.registry, CTX, h.emit)

    await h.loop.run(h.state, TurnTimer(), system=["prompt"], tools=["hangs"])

    errors = [e for e in h.events if isinstance(e, protocol.Error)]
    assert [(e.code, e.retryable) for e in errors] == [("tool_failed", True)]
    assert h.tool_results() == [
        {"error": "timeout", "message": "Tool 'hangs' did not answer in time."}
    ]


# --- the executor ----------------------------------------------------------------------------


async def test_cancelled_turn_still_clears_an_old_pending_action() -> None:
    flow = FlowGraph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "data": {"greeting": "Привіт!"}},
                {"id": "ask", "type": "llm_prompt", "data": {"prompt": "Допомагай."}},
            ],
            "edges": [{"source": "start", "target": "ask"}],
        }
    )
    provider = FakeProvider([[Delay(10), TextDelta(text="пізно")]])
    registry = ToolRegistry()

    async def emit(_: protocol.ProtocolMessage) -> None:
        return None

    loop = ToolLoop(provider, "m", registry, CTX, emit)
    executor = FlowExecutor(flow, loop, registry, CTX, emit)
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)
    await executor.start(state, TurnTimer())
    # A price was quoted in turn 2; turn 3 gets interrupted before any answer.
    state.turn = 2
    state.pending_action = PendingAction(name="activate_service", args={}, turn=2)

    turn = asyncio.create_task(executor.handle(state, "е-е…", TurnTimer()))
    await asyncio.sleep(0.01)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert state.pending_action is None
