"""Flow executor tests; the walkthrough tests replay the SDD "Прогін на папері" table."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, JsonValue

from rozmova_contracts import protocol
from rozmova_contracts.flow import FlowGraph, KBAnswerNode
from rozmova_engine.flow.executor import (
    HANDOFF_FLOW_ERROR,
    HANDOFF_TRANSITION_LIMIT,
    FlowExecutor,
    KBReply,
    TurnOutcome,
)
from rozmova_engine.providers import Message, ToolCall
from rozmova_engine.providers.fake import FakeProvider, Step, text_turn
from rozmova_engine.session import HistoryEntry, PendingAction, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ROUTE_TOOL, ToolLoop
from rozmova_engine.tools import (
    CONFIRMATION_REQUIRED,
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    demo_subscribers,
    subscriber_tools,
)

DEMO_FLOW = FlowGraph.model_validate_json(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)
GREETING = "Вітаю! Я асистент Hvylia Mobile. Чим можу допомогти?"
NOT_FOUND = "У базі знань немає відповіді на це питання. Хочете, переведу на оператора?"
BEZLIM_ANSWER = "Тариф Безлім описано у фрагментах [1] і [2]."
BEZLIM_SOURCES = [
    protocol.Source(marker=1, chunk_id="bezlim#1", document_id="bezlim", document_title="Безлім"),
    protocol.Source(marker=2, chunk_id="bezlim#2", document_id="bezlim", document_title="Безлім"),
]


def route(name: str) -> list[Step]:
    return [ToolCall(id=f"route-{name}", name=ROUTE_TOOL, arguments={"name": name})]


def tool(name: str, **args: JsonValue) -> list[Step]:
    return [ToolCall(id=f"call-{name}", name=name, arguments=args)]


class FakeKB:
    """Answers about «Безлім» with two cited chunks; anything else is not found."""

    def __init__(self, emit: Any) -> None:
        self.emit = emit
        self.queries: list[str] = []

    async def answer(self, node: KBAnswerNode, state: SessionState, timer: TurnTimer) -> KBReply:
        query = next(e.message.content for e in reversed(state.history) if e.message.role == "user")
        self.queries.append(query)
        with timer.stage("rag.search"):
            found = "безлім" in query.lower()
        text = BEZLIM_ANSWER if found else node.data.not_found_reply
        timer.first_text()
        await self.emit(protocol.AssistantDelta(turn_id=state.turn, text=text))
        message = Message(role="assistant", content=text)
        state.history.append(HistoryEntry(turn=state.turn, message=message))
        return KBReply(text, BEZLIM_SOURCES if found else [])


class Call:
    """One call on a flow: scripted LLM, mock billing, fake KB, recorded events."""

    def __init__(
        self,
        *llm_calls: list[Step],
        flow: FlowGraph = DEMO_FLOW,
        tools: list[Tool[Any]] | None = None,
    ) -> None:
        """``tools`` replaces the mock subscriber tools when given."""
        self.provider = FakeProvider(llm_calls)
        self.billing = MockBilling(demo_subscribers())
        registry = ToolRegistry(tools if tools is not None else subscriber_tools(self.billing))
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        self.events: list[protocol.ProtocolMessage] = []
        self.kb = FakeKB(self.emit)
        loop = ToolLoop(self.provider, "gpt-4.1-mini", registry, ctx, self.emit)
        self.executor = FlowExecutor(flow, loop, registry, ctx, self.emit, self.kb)
        self.state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        self.events.append(event)

    async def start(self) -> TurnOutcome:
        self.events.clear()
        return await self.executor.start(self.state, TurnTimer())

    async def say(self, utterance: str) -> TurnOutcome:
        self.events.clear()
        return await self.executor.handle(self.state, utterance, TurnTimer())

    def types(self) -> list[str]:
        return [str(getattr(event, "type", "")) for event in self.events]

    def entered(self) -> list[str]:
        return [e.node_id for e in self.events if isinstance(e, protocol.NodeEnter)]


# Read state through functions so mypy does not carry narrowing from one turn to the next.
def pending(call: Call) -> PendingAction | None:
    return call.state.pending_action


def node(call: Call) -> str | None:
    return call.state.current_node


# --- the SDD paper walkthrough ---------------------------------------------------------


async def test_walkthrough_start_s1_s2_s3_s4() -> None:
    call = Call(
        tool("get_balance"),
        text_turn("На рахунку 120,50 грн."),
        route("tariff"),
        route("tariff"),
        tool("activate_service", service_id="roaming_eu"),
        text_turn("Роумінг Європа — 99 грн/міс. Підключити?"),
        tool("activate_service", service_id="roaming_eu"),
        text_turn("Готово, послугу підключено."),
        text_turn("Добре, нічого не підключаю."),
        route("operator"),
        text_turn("Перед переведенням: хочете новини про тарифи?"),
        route("done"),
    )

    # старт: start → greeting → stop at assist
    start = await call.start()
    assert start.text == GREETING
    assert call.types() == ["node_enter", "assistant_delta"]
    assert (call.state.turn, node(call)) == (1, "assist")

    # S1: assist → get_balance → reply
    s1 = await call.say("скільки в мене грошей")
    assert s1.text == "На рахунку 120,50 грн."
    assert call.types()[:3] == ["node_enter", "tool_call", "tool_result"]
    assert node(call) == "assist"

    # S2: route('tariff') → tariff_kb with the same utterance → cited answer → back to assist
    s2 = await call.say("що входить у тариф Безлім?")
    assert call.entered() == ["assist", "tariff_kb"]
    assert call.kb.queries[-1] == "що входить у тариф Безлім?"
    assert s2.sources == BEZLIM_SOURCES
    assert node(call) == "assist"

    # S2, not in the KB: not_found_reply, no sources
    s2_missing = await call.say("що входить у тариф Космос?")
    assert (s2_missing.text, s2_missing.sources) == (NOT_FOUND, [])
    assert node(call) == "assist"

    # S3 step 1: quote, pending_action stored
    await call.say("підключіть роумінг Європа")
    quote = next(e for e in call.events if isinstance(e, protocol.ToolResult))
    assert quote.result["status"] == CONFIRMATION_REQUIRED  # type: ignore[index, call-overload]
    assert pending(call) is not None

    # S3 step 2: «так» in the next turn activates
    s3 = await call.say("так")
    assert s3.text == "Готово, послугу підключено."
    assert "roaming_eu" in call.billing.find(DEMO_SUBSCRIBER_ID).services  # type: ignore[union-attr]
    assert pending(call) is None

    # S3 refusal: a plain reply, nothing pending, still at assist
    await call.say("ні, не треба")
    assert "tool_call" not in call.types()
    assert node(call) == "assist"

    # S4/FR-5: route('operator') → promo with the same utterance; promo stays current
    s4 = await call.say("дайте оператора")
    assert call.entered() == ["assist", "promo"]
    assert s4.text == "Перед переведенням: хочете новини про тарифи?"
    assert node(call) == "promo"

    # S4, next: route('done') → operator handoff, the call ends
    s4_end = await call.say("ні")
    assert call.entered() == ["promo", "operator"]
    assert call.events[-1] == protocol.Handoff(reason="requested_or_unresolved")
    assert s4_end.ended == "handoff"
    assert node(call) is None
    assert call.provider.remaining_turns == 0


async def test_walkthrough_goodbye_ends_the_call() -> None:
    call = Call(route("bye"))
    await call.start()

    outcome = await call.say("дякую, до побачення")

    assert call.entered() == ["assist", "bye"]
    assert outcome.text == "Дякую за дзвінок. Гарного дня!"
    assert outcome.ended == "completed"
    assert call.state.current_node is None


async def test_the_llm_sees_the_node_prompt_and_the_utterance_after_a_route() -> None:
    call = Call(route("operator"), text_turn("Хочете новини?"))
    await call.start()
    await call.say("дайте оператора")

    assist_call, promo_call = call.provider.calls
    assert assist_call.messages[0].content.startswith("Ти асистент оператора Hvylia Mobile.")
    assert promo_call.messages[0].content.startswith("Перед переведенням на оператора")
    assert promo_call.messages[-1] == Message(role="user", content="дайте оператора")
    assert [spec.name for spec in promo_call.tools] == [ROUTE_TOOL]


async def test_pending_action_from_a_refused_quote_is_cleared() -> None:
    call = Call(
        tool("activate_service", service_id="music"),
        text_turn("Хвиля Музика — 49 грн/міс. Підключити?"),
        text_turn("Добре."),
    )
    await call.start()
    await call.say("підключи музику")
    await call.say("ні, не треба")
    assert call.state.pending_action is None


async def test_greeting_is_turn_one_in_history() -> None:
    call = Call()
    await call.start()
    [entry] = call.state.history
    assert (entry.turn, entry.message.content) == (1, GREETING)
    assert call.events[1] == protocol.AssistantDelta(turn_id=1, text=GREETING)


# --- tool and condition nodes ----------------------------------------------------------


def flow(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> FlowGraph:
    return FlowGraph.model_validate({"nodes": nodes, "edges": edges})


START = {"id": "start", "type": "start", "data": {"greeting": "Привіт!"}}
ASK = {"id": "ask", "type": "llm_prompt", "data": {"prompt": "Спитай, чим допомогти."}}


def low_balance_flow() -> FlowGraph:
    """start → balance (tool) → check (condition) → low: end, otherwise ask."""
    return flow(
        [
            START,
            {
                "id": "balance",
                "type": "tool",
                "data": {"tool": "get_balance", "args": {}, "save_as": "account"},
            },
            {
                "id": "check",
                "type": "condition",
                "data": {
                    "cases": [
                        {
                            "when": {"<": [{"var": "account.balance_uah"}, 10]},
                            "route": "low",
                        }
                    ]
                },
            },
            {"id": "low", "type": "end", "data": {"farewell": "Поповніть рахунок, будь ласка."}},
            ASK,
        ],
        [
            {"source": "start", "target": "balance"},
            {"source": "balance", "target": "check"},
            {"source": "check", "target": "low", "when": {"route": "low"}},
            {"source": "check", "target": "ask"},
        ],
    )


class Balance(BaseModel):
    pass


def numeric_balance(value: float) -> Tool[Any]:
    """Replaces the mock get_balance with one that returns a number, for the condition."""

    async def handler(_: Balance, __: ToolContext) -> ToolResult:
        return {"balance_uah": value}

    return Tool("get_balance", "", Balance, handler)


async def test_tool_node_fills_a_slot_and_condition_branches_on_it() -> None:
    call = Call(flow=low_balance_flow(), tools=[numeric_balance(3.2)])

    outcome = await call.start()

    assert call.entered() == ["start", "balance", "check", "low"]
    assert call.state.slots["account"] == {"balance_uah": 3.2}
    assert outcome.text == "Привіт! Поповніть рахунок, будь ласка."
    assert outcome.ended == "completed"


async def test_condition_falls_back_to_the_unconditional_edge() -> None:
    call = Call(flow=low_balance_flow(), tools=[numeric_balance(120.5)])

    outcome = await call.start()

    assert call.entered() == ["start", "balance", "check"]
    assert call.state.current_node == "ask"
    assert outcome.ended is None


async def test_tool_node_arguments_are_templates_over_slots() -> None:
    seen: list[dict[str, Any]] = []

    class Args(BaseModel):
        service_id: str
        note: str

    async def handler(args: Args, _: ToolContext) -> ToolResult:
        seen.append(args.model_dump())
        return {"ok": True}

    call = Call(
        flow=flow(
            [
                START,
                {
                    "id": "remember",
                    "type": "tool",
                    "data": {
                        "tool": "note",
                        "args": {"service_id": "{offer.id}", "note": "для {name}"},
                    },
                },
                ASK,
            ],
            [
                {"source": "start", "target": "remember"},
                {"source": "remember", "target": "ask"},
            ],
        ),
        tools=[Tool("note", "", Args, handler)],
    )
    call.state.slots.update({"offer": {"id": "music"}, "name": "Олена"})

    await call.start()

    assert seen == [{"service_id": "music", "note": "для Олена"}]


# --- guards ------------------------------------------------------------------------------


async def test_transition_limit_ends_in_error_and_handoff() -> None:
    looping = flow(
        [
            START,
            {"id": "a", "type": "condition", "data": {"cases": []}},
            {"id": "b", "type": "condition", "data": {"cases": []}},
        ],
        [
            {"source": "start", "target": "a"},
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
        ],
    )
    call = Call(flow=looping)

    outcome = await call.start()

    assert call.types()[-2:] == ["error", "handoff"]
    assert call.events[-1] == protocol.Handoff(reason=HANDOFF_TRANSITION_LIMIT)
    assert outcome.ended == "handoff"


async def test_missing_edge_is_a_flow_error_with_handoff() -> None:
    # Validation rejects such a flow before publish; the executor still must not crash.
    broken = DEMO_FLOW.model_copy(
        update={"edges": [e for e in DEMO_FLOW.edges if e.when is None or e.when.route != "tariff"]}
    )
    call = Call(route("tariff"), flow=broken)
    await call.start()

    outcome = await call.say("що входить у тариф?")

    assert call.events[-1] == protocol.Handoff(reason=HANDOFF_FLOW_ERROR)
    assert outcome.ended == "handoff"


async def test_tool_loop_limit_hands_off_through_the_executor() -> None:
    ping = tool("get_balance")
    call = Call(ping, ping, ping, ping)
    await call.start()

    outcome = await call.say("перевір усе")

    assert call.events[-1] == protocol.Handoff(reason="tool_loop_limit")
    assert outcome.ended == "handoff"


async def test_turn_after_the_call_ended_is_rejected() -> None:
    call = Call(route("bye"))
    await call.start()
    await call.say("бувай")

    with pytest.raises(ValueError, match="ended"):
        await call.say("ще одне")
