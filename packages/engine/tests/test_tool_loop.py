import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from rozmova_contracts import protocol
from rozmova_engine.providers import Message, ProviderError, TextDelta, ToolCall, Usage
from rozmova_engine.providers.fake import Delay, FakeProvider, text_turn
from rozmova_engine.session import HistoryEntry, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import HANDOFF_TOOL_LOOP_LIMIT, ROUTE_TOOL, ToolLoop
from rozmova_engine.tools import (
    CONFIRMATION_REQUIRED,
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    demo_subscribers,
    finish_turn,
    subscriber_tools,
)

SYSTEM = ["Ти асистент оператора Hvylia Mobile.", "Відповідай коротко."]
ASSIST_TOOLS = ["get_balance", "get_current_tariff", "list_services", "activate_service"]
BALANCE = ToolCall(id="c1", name="get_balance")


class Harness:
    """A ToolLoop over FakeProvider and the mock billing, recording emitted events."""

    def __init__(
        self, *turns: list[Any], tools: list[Tool[Any]] | None = None, **loop: Any
    ) -> None:
        self.provider = FakeProvider(turns)
        self.billing = MockBilling(demo_subscribers())
        self.registry = ToolRegistry([*subscriber_tools(self.billing), *(tools or [])])
        self.state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)
        self.events: list[protocol.ProtocolMessage] = []
        self.loop = ToolLoop(
            self.provider,
            "gpt-4.1-mini",
            self.registry,
            ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID),
            self.emit,
            **loop,
        )

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        self.events.append(event)

    def user_says(self, text: str) -> None:
        """Start the next turn the way the executor will: bump the turn, record the utterance."""
        if self.state.turn:
            finish_turn(self.state)
        self.state.turn += 1
        message = Message(role="user", content=text)
        self.state.history.append(HistoryEntry(turn=self.state.turn, message=message))

    def types(self) -> list[str]:
        return [str(getattr(event, "type", "")) for event in self.events]


async def run(h: Harness, timer: TurnTimer | None = None, **kwargs: Any) -> Any:
    kwargs.setdefault("tools", ASSIST_TOOLS)
    return await h.loop.run(h.state, timer or TurnTimer(), system=SYSTEM, **kwargs)


# --- S1: tool round trip ---------------------------------------------------------------


async def test_tool_result_goes_back_to_the_llm_and_the_reply_is_streamed() -> None:
    h = Harness([BALANCE], text_turn("На рахунку 120,50 грн.", chunk_size=10))
    h.user_says("скільки в мене грошей")

    result = await run(h)

    assert (result.outcome, result.text) == ("reply", "На рахунку 120,50 грн.")
    assert h.types() == [
        "tool_call",
        "tool_result",
        "assistant_delta",
        "assistant_delta",
        "assistant_delta",
    ]
    assert h.events[1] == protocol.ToolResult(
        turn_id=1, name="get_balance", result={"balance": "120.50", "currency": "грн"}
    )

    first, second = h.provider.calls
    assert [m.content for m in first.messages[:2]] == SYSTEM
    assert [spec.name for spec in first.tools] == ASSIST_TOOLS
    tool_message = second.messages[-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == {"balance": "120.50", "currency": "грн"}


async def test_turn_is_recorded_in_history() -> None:
    h = Harness([BALANCE], text_turn("120,50 грн."))
    h.user_says("баланс?")
    await run(h)

    assert [(e.turn, e.message.role) for e in h.state.history] == [
        (1, "user"),
        (1, "assistant"),
        (1, "tool"),
        (1, "assistant"),
    ]
    assert h.state.history[1].message.tool_calls == (BALANCE,)
    assert h.state.history[-1].message.content == "120,50 грн."


async def test_several_tool_calls_in_one_response_run_in_order() -> None:
    tariff = ToolCall(id="c2", name="get_current_tariff")
    h = Harness([BALANCE, tariff], text_turn("Оптимум, 120,50 грн."))
    h.user_says("баланс і тариф")
    await run(h)

    calls = [e for e in h.events if isinstance(e, protocol.ToolCall)]
    assert [c.name for c in calls] == ["get_balance", "get_current_tariff"]
    assert [m.role for m in h.provider.calls[1].messages[-2:]] == ["tool", "tool"]


# --- S3 through the loop ---------------------------------------------------------------


async def test_confirmation_rule_applies_inside_the_loop() -> None:
    activate = ToolCall(id="a1", name="activate_service", arguments={"service_id": "roaming_eu"})
    h = Harness(
        [activate],
        text_turn("Роумінг Європа — 99 грн/міс. Підключити?"),
        [activate],
        text_turn("Готово, підключено."),
    )
    h.user_says("підключіть роумінг Європа")
    await run(h)
    quoted = [e for e in h.events if isinstance(e, protocol.ToolResult)][-1]
    assert quoted.result["status"] == CONFIRMATION_REQUIRED  # type: ignore[index, call-overload]

    h.user_says("так")
    await run(h)
    activated = [e for e in h.events if isinstance(e, protocol.ToolResult)][-1]
    assert activated.result["status"] == "activated"  # type: ignore[index, call-overload]


# --- routes ------------------------------------------------------------------------------


async def test_route_call_ends_the_step_without_a_trace_in_history() -> None:
    route = ToolCall(id="r1", name=ROUTE_TOOL, arguments={"name": "tariff"})
    h = Harness([route])
    h.user_says("що входить у тариф Безлім?")

    result = await run(h, routes=["tariff", "operator", "bye"])

    assert (result.outcome, result.route) == ("route", "tariff")
    assert h.events == []
    assert [e.message.role for e in h.state.history] == ["user"]
    route_spec = h.provider.calls[0].tools[-1]
    assert route_spec.name == ROUTE_TOOL
    assert route_spec.parameters["properties"] == {
        "name": {"type": "string", "enum": ["tariff", "operator", "bye"]}
    }


async def test_unknown_route_is_returned_to_the_llm() -> None:
    wrong = ToolCall(id="r1", name=ROUTE_TOOL, arguments={"name": "billing"})
    right = ToolCall(id="r2", name=ROUTE_TOOL, arguments={"name": "operator"})
    h = Harness([wrong], [right])
    h.user_says("хочу оператора")

    result = await run(h, routes=["tariff", "operator"])

    assert result.route == "operator"
    error = next(e for e in h.events if isinstance(e, protocol.ToolResult))
    assert error.result["error"] == "unknown_route"  # type: ignore[index, call-overload]


async def test_no_route_tool_without_routes() -> None:
    h = Harness(text_turn("Привіт!"))
    h.user_says("привіт")
    await run(h)
    assert ROUTE_TOOL not in [spec.name for spec in h.provider.calls[0].tools]


# --- limits ----------------------------------------------------------------------------


async def test_fourth_request_for_tools_ends_in_handoff_without_executing() -> None:
    executed: list[str] = []

    class NoArgs(BaseModel):
        pass

    async def counted(_: NoArgs, __: ToolContext) -> ToolResult:
        executed.append("ping")
        return {"ok": True}

    ping = ToolCall(id="p", name="ping")
    h = Harness([ping], [ping], [ping], [ping], tools=[Tool("ping", "", NoArgs, counted)])
    h.user_says("зроби щось")

    result = await run(h, tools=["ping"])

    assert result.outcome == "handoff"
    assert result.handoff_reason == HANDOFF_TOOL_LOOP_LIMIT
    assert len(h.provider.calls) == 4
    assert executed == ["ping", "ping", "ping"]
    # The unexecuted calls of the 4th response are not left dangling in history.
    assert h.state.history[-1].message.role == "tool"


async def test_first_token_timeout_fails_the_step() -> None:
    h = Harness([Delay(1.0), TextDelta(text="пізно")], first_token_timeout=0.01)
    h.user_says("алло")
    with pytest.raises(ProviderError, match="no first token") as info:
        await run(h)
    assert info.value.retryable


async def test_tool_failure_is_reported_to_the_llm_and_the_loop_goes_on() -> None:
    class NoArgs(BaseModel):
        pass

    async def broken(_: NoArgs, __: ToolContext) -> ToolResult:
        raise RuntimeError("billing down")

    h = Harness(
        [ToolCall(id="b", name="broken")],
        text_turn("Не вдалося перевірити, спробуйте пізніше."),
        tools=[Tool("broken", "", NoArgs, broken)],
    )
    h.user_says("баланс")
    result = await run(h, tools=["broken"])

    assert result.outcome == "reply"
    assert json.loads(h.provider.calls[1].messages[-1].content)["error"] == "tool_failed"


# --- timings and usage -------------------------------------------------------------------


async def test_timings_cover_llm_and_tool_stages() -> None:
    h = Harness(
        [Delay(0.02), BALANCE, Usage(input_tokens=100, output_tokens=5)],
        [Delay(0.02), TextDelta(text="120,50 грн."), Usage(input_tokens=130, output_tokens=8)],
    )
    h.user_says("баланс")
    timer = TurnTimer()

    result = await run(h, timer)
    timings = timer.to_timings()

    assert set(timings.stages) == {"llm_ttft", "llm_total", "tool.get_balance"}
    assert timings.stages["llm_ttft"] >= 40  # two calls, 20 ms each before the first event
    assert timings.stages["llm_total"] >= timings.stages["llm_ttft"]
    assert timings.ttft_ms is not None
    assert timings.ttft_ms <= timings.total_ms
    assert result.usage == Usage(input_tokens=230, output_tokens=13)


def test_timer_adds_repeated_stages_and_keeps_the_first_text_mark() -> None:
    now = [0.0]
    timer = TurnTimer(clock=lambda: now[0])
    with timer.stage("tool.get_balance"):
        now[0] += 0.010
    with timer.stage("tool.get_balance"):
        now[0] += 0.005
    timer.first_text()
    now[0] += 0.100
    timer.first_text()

    timings = timer.to_timings()
    assert timings.stages == {"tool.get_balance": 15.0}
    assert timings.ttft_ms == 15.0
    assert timings.total_ms == 115.0


def test_reply_without_text_has_no_ttft() -> None:
    assert TurnTimer().to_timings().ttft_ms is None
