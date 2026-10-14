"""Flow executor: runs a published ``FlowGraph`` turn by turn (SDD "Флоу", FR-6).

Nodes are *waiting* (``llm_prompt``, ``kb_answer``: they consume the subscriber's
utterance and reply) or *pass-through* (``start``, ``tool``, ``condition``, ``handoff``,
``end``: they run at once). Both entry points walk the graph with one rule set:

- a waiting node with an unconsumed utterance consumes it; without one, the walk stops
  there and the node becomes ``current_node``;
- ``route(name)`` from an ``llm_prompt`` follows the ``when.route == name`` edge in the
  same turn, with the same utterance still to be consumed;
- after a reply the walk follows the node's unconditional edge and keeps running
  pass-through nodes, so a reply followed by ``end`` says goodbye in the same turn; with
  no unconditional edge the replying node stays current;
- ``handoff`` and ``end`` finish the call; more than 20 transitions in one turn is a
  broken flow and ends in ``error`` + ``handoff``.

The executor emits ``node_enter`` for every node it runs, plus the events of the nodes
themselves; ``session_end`` is the gateway's, from ``TurnOutcome.ended``.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from rozmova_contracts import protocol
from rozmova_contracts.flow import (
    ConditionNode,
    EndNode,
    FlowGraph,
    FlowNode,
    HandoffNode,
    KBAnswerNode,
    LLMPromptNode,
    StartNode,
    ToolNode,
)
from rozmova_engine.flow.jsonlogic import JSONLogicError, evaluate, truthy
from rozmova_engine.providers import Message, ToolCall, Usage
from rozmova_engine.session import HistoryEntry, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import ToolContext, ToolRegistry, call_tool, finish_turn

Emit = Callable[[protocol.ProtocolMessage], Awaitable[None]]

MAX_TRANSITIONS = 20
HANDOFF_TRANSITION_LIMIT = "flow_transition_limit"
HANDOFF_FLOW_ERROR = "flow_error"

_WAITING = (LLMPromptNode, KBAnswerNode)
_WHOLE_SLOT = re.compile(r"\{([\w.]+)\}")


@dataclass(frozen=True, slots=True)
class KBReply:
    text: str
    sources: list[protocol.Source] = field(default_factory=list)


class KBAnswerer(Protocol):
    """Answers a ``kb_answer`` node from the knowledge base (ROZ-43).

    It streams its reply through ``emit`` itself and returns the text and the cited
    sources; the executor records the reply in history. The query is the last user
    message in ``state.history``.
    """

    async def answer(
        self, node: KBAnswerNode, state: SessionState, timer: TurnTimer
    ) -> KBReply: ...


@dataclass(slots=True)
class TurnOutcome:
    """What one turn produced, for ``turn_end`` and for the gateway."""

    text: str = ""
    sources: list[protocol.Source] = field(default_factory=list)
    ended: protocol.SessionEndReason | None = None
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=0, output_tokens=0))

    def add_text(self, text: str) -> None:
        if text:
            self.text = f"{self.text} {text}" if self.text else text

    def add_usage(self, usage: Usage) -> None:
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + usage.input_tokens,
            output_tokens=self.usage.output_tokens + usage.output_tokens,
        )


class FlowError(Exception):
    """The flow cannot continue: a missing edge or an invalid condition rule."""


class FlowExecutor:
    """One per session; holds the flow and the services its nodes use."""

    def __init__(
        self,
        flow: FlowGraph,
        loop: ToolLoop,
        registry: ToolRegistry,
        ctx: ToolContext,
        emit: Emit,
        kb: KBAnswerer | None = None,
    ) -> None:
        self._nodes = {node.id: node for node in flow.nodes}
        self._default = {e.source: e.target for e in flow.edges if e.when is None}
        self._routes = {(e.source, e.when.route): e.target for e in flow.edges if e.when}
        self._loop = loop
        self._registry = registry
        self._ctx = ctx
        self._emit = emit
        self._kb = kb

    async def start(self, state: SessionState, timer: TurnTimer) -> TurnOutcome:
        """Begin the call: greeting and pass-through nodes up to the first waiting node."""
        if state.turn != 0:
            raise ValueError("the call has already started")
        start = next((n.id for n in self._nodes.values() if isinstance(n, StartNode)), None)
        if start is None:
            raise FlowError("the flow has no start node")
        state.turn = 1
        outcome = await self._walk(state, start, utterance=False, timer=timer)
        finish_turn(state)
        return outcome

    async def handle(self, state: SessionState, utterance: str, timer: TurnTimer) -> TurnOutcome:
        """Run one subscriber turn from ``current_node``."""
        if state.current_node is None:
            raise ValueError("the call has not started or has already ended")
        state.turn += 1
        user = Message(role="user", content=utterance)
        state.history.append(HistoryEntry(turn=state.turn, message=user))
        outcome = await self._walk(state, state.current_node, utterance=True, timer=timer)
        finish_turn(state)
        return outcome

    async def _walk(
        self, state: SessionState, node_id: str, *, utterance: bool, timer: TurnTimer
    ) -> TurnOutcome:
        outcome = TurnOutcome()
        try:
            for _ in range(MAX_TRANSITIONS):
                node = self._node(node_id)
                if isinstance(node, _WAITING) and not utterance:
                    state.current_node = node.id
                    return outcome
                await self._emit(protocol.NodeEnter(node_id=node.id))

                match node:
                    case StartNode():
                        await self._say(state, node.data.greeting, outcome, timer)
                        node_id = self._next(node.id)
                    case LLMPromptNode():
                        utterance = False
                        result = await self._loop.run(
                            state,
                            timer,
                            system=[node.data.prompt],
                            tools=node.data.tools,
                            routes=node.data.routes,
                        )
                        outcome.add_usage(result.usage)
                        outcome.add_text(result.text)
                        if result.outcome == "handoff":
                            return await self._handoff(
                                state, result.handoff_reason or "tool_loop_limit", outcome
                            )
                        if result.outcome == "route":
                            node_id = self._route(node.id, result.route or "")
                            utterance = True
                            continue
                        if (after := self._default.get(node.id)) is None:
                            state.current_node = node.id
                            return outcome
                        node_id = after
                    case KBAnswerNode():
                        utterance = False
                        reply = await self._answer_from_kb(node, state, timer)
                        outcome.add_text(reply.text)
                        outcome.sources.extend(reply.sources)
                        if (after := self._default.get(node.id)) is None:
                            state.current_node = node.id
                            return outcome
                        node_id = after
                    case ToolNode():
                        await self._run_tool_node(node, state, timer)
                        node_id = self._next(node.id)
                    case ConditionNode():
                        node_id = self._branch(node, state)
                    case HandoffNode():
                        return await self._handoff(state, node.data.reason, outcome)
                    case EndNode():
                        await self._say(state, node.data.farewell, outcome, timer)
                        state.current_node = None
                        outcome.ended = "completed"
                        return outcome
        except FlowError as err:
            return await self._fail(state, str(err), HANDOFF_FLOW_ERROR, outcome)
        return await self._fail(
            state,
            f"more than {MAX_TRANSITIONS} transitions in one turn",
            HANDOFF_TRANSITION_LIMIT,
            outcome,
        )

    # --- nodes ---------------------------------------------------------------------

    async def _say(
        self, state: SessionState, text: str, outcome: TurnOutcome, timer: TurnTimer
    ) -> None:
        """A fixed text from the flow (greeting, farewell): streamed and recorded."""
        timer.first_text()
        await self._emit(protocol.AssistantDelta(turn_id=state.turn, text=text))
        state.history.append(
            HistoryEntry(turn=state.turn, message=Message(role="assistant", content=text))
        )
        outcome.add_text(text)

    async def _answer_from_kb(
        self, node: KBAnswerNode, state: SessionState, timer: TurnTimer
    ) -> KBReply:
        if self._kb is None:
            raise FlowError(f"node {node.id!r} needs the knowledge base, which is not set up")
        reply = await self._kb.answer(node, state, timer)
        if reply.text:
            message = Message(role="assistant", content=reply.text)
            state.history.append(HistoryEntry(turn=state.turn, message=message))
        return reply

    async def _run_tool_node(self, node: ToolNode, state: SessionState, timer: TurnTimer) -> None:
        args = {name: _render(value, state.slots) for name, value in node.data.args.items()}
        call = ToolCall(id=f"{node.id}:{state.turn}", name=node.data.tool, arguments=args)
        await self._emit(protocol.ToolCall(turn_id=state.turn, name=call.name, args=args))
        with timer.stage(f"tool.{call.name}"):
            result = await call_tool(self._registry, call, self._ctx, state)
        await self._emit(protocol.ToolResult(turn_id=state.turn, name=call.name, result=result))
        if node.data.save_as:
            state.slots[node.data.save_as] = result

    def _branch(self, node: ConditionNode, state: SessionState) -> str:
        for case in node.data.cases:
            try:
                matched = truthy(evaluate(case.when, state.slots))
            except JSONLogicError as err:
                raise FlowError(f"node {node.id!r}: {err}") from err
            if matched:
                return self._route(node.id, case.route)
        return self._next(node.id)

    async def _handoff(self, state: SessionState, reason: str, outcome: TurnOutcome) -> TurnOutcome:
        await self._emit(protocol.Handoff(reason=reason))
        state.current_node = None
        outcome.ended = "handoff"
        return outcome

    async def _fail(
        self, state: SessionState, message: str, reason: str, outcome: TurnOutcome
    ) -> TurnOutcome:
        await self._emit(protocol.Error(code="internal", message=f"Flow error: {message}"))
        return await self._handoff(state, reason, outcome)

    # --- graph ---------------------------------------------------------------------

    def _node(self, node_id: str) -> FlowNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise FlowError(f"no node {node_id!r}") from None

    def _next(self, node_id: str) -> str:
        try:
            return self._default[node_id]
        except KeyError:
            raise FlowError(f"node {node_id!r} has no unconditional edge") from None

    def _route(self, node_id: str, route: str) -> str:
        try:
            return self._routes[(node_id, route)]
        except KeyError:
            raise FlowError(f"node {node_id!r} has no edge for route {route!r}") from None


def _render(value: JsonValue, slots: dict[str, JsonValue]) -> JsonValue:
    """Fill ``{slot}`` placeholders in string arguments from the session slots.

    A value that is exactly ``"{name}"`` takes the slot's value as is (a number stays a
    number); placeholders inside longer strings are replaced with text. Dotted names reach
    into objects; a missing slot renders as ``None`` or an empty string.
    """
    if not isinstance(value, str):
        return value
    if whole := _WHOLE_SLOT.fullmatch(value):
        return _lookup(slots, whole.group(1))
    return _WHOLE_SLOT.sub(lambda m: _text(_lookup(slots, m.group(1))), value)


def _lookup(slots: dict[str, JsonValue], path: str) -> JsonValue:
    value: JsonValue = slots
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _text(value: JsonValue) -> str:
    return "" if value is None else str(value)
