"""The function-calling loop of one ``llm_prompt`` step (SDD "LLM-шар").

LLM → tool calls → results back to the LLM → … until the model answers with text, asks
for a route, or runs out of iterations. Text is forwarded through ``emit`` as soon as it
arrives; tool calls go through ``call_tool``, so two-phase tools keep their confirmation
rule. Everything the step adds to the conversation is appended to ``state.history``.

Cancellation (a new utterance, FR-4) can arrive at any await. The loop keeps the history
valid for the next turn: text already streamed is stored as an ``interrupted`` reply, a
side-effect tool that already started is finished under ``shield`` and its real result is
recorded, and tool calls that did not run get a ``cancelled`` result, because providers
reject a tool call without a result. Then the cancellation propagates.

Limits:

- at most ``max_iterations`` LLM calls (4); if the last one still asks for tools, those
  calls are not executed (the model would never see their results) and the loop ends with
  a handoff, which the executor turns into a ``handoff`` event;
- every LLM call must yield its first event within ``first_token_timeout`` (10 s).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Literal

from rozmova_contracts import protocol
from rozmova_engine.providers import (
    LLMProvider,
    Message,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from rozmova_engine.session import HistoryEntry, SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tools import ToolContext, ToolRegistry, ToolResult, call_tool, tool_error
from rozmova_engine.tools.shield import complete_despite_cancel

Emit = Callable[[protocol.ProtocolMessage], Awaitable[None]]

MAX_ITERATIONS = 4
FIRST_TOKEN_TIMEOUT = 10.0

# The service tool through which the LLM leaves the node along a `when.route` edge.
ROUTE_TOOL = "route"
HANDOFF_TOOL_LOOP_LIMIT = "tool_loop_limit"

# Tool results that also reach the client as an `error` event (SDD: timeouts are typed errors).
_FAILED = frozenset({"timeout", "tool_failed"})


@dataclass(frozen=True, slots=True)
class LoopResult:
    """How the step ended: a reply, a route to follow, or a handoff to an operator."""

    outcome: Literal["reply", "route", "handoff"]
    text: str = ""
    route: str | None = None
    handoff_reason: str | None = None
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=0, output_tokens=0))


def route_spec(routes: Sequence[str]) -> ToolSpec:
    """The ``route`` tool offered to the LLM when the node has routes."""
    return ToolSpec(
        name=ROUTE_TOOL,
        description=(
            "Передає розмову іншому кроку сценарію. Виклич замість відповіді, коли "
            "питання належить одному з маршрутів; абонент не повторюватиме репліку."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(routes)}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )


class ToolLoop:
    """Runs LLM steps for one session; the dependencies stay the same for every turn."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        registry: ToolRegistry,
        ctx: ToolContext,
        emit: Emit,
        *,
        max_iterations: int = MAX_ITERATIONS,
        first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    ) -> None:
        self._provider = provider
        self._model = model
        self._registry = registry
        self._ctx = ctx
        self._emit = emit
        self._max_iterations = max_iterations
        self._first_token_timeout = first_token_timeout

    async def run(
        self,
        state: SessionState,
        timer: TurnTimer,
        *,
        system: Sequence[str],
        tools: Sequence[str] = (),
        routes: Sequence[str] = (),
    ) -> LoopResult:
        """Run one step in turn ``state.turn``; the user utterance is already in history.

        ``system`` holds the bot and node prompts, ``tools`` the node's allowed tools and
        ``routes`` its route names.
        """
        specs = self._registry.specs(tools)
        if routes:
            specs.append(route_spec(routes))
        prompts = [Message(role="system", content=text) for text in system]
        tokens_in = tokens_out = 0

        for iteration in range(1, self._max_iterations + 1):
            messages = prompts + state.llm_messages()
            text, calls, usage = await self._call_llm(messages, specs, state, timer)
            if usage is not None:
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
            total = Usage(input_tokens=tokens_in, output_tokens=tokens_out)

            route = next((c for c in calls if c.name == ROUTE_TOOL), None)
            if route is not None and route.arguments.get("name") in routes:
                # The route call is engine-internal: it is not kept in history, so the next
                # node's LLM never sees a tool call without a result.
                self._remember(state, Message(role="assistant", content=text))
                return LoopResult("route", text, route=str(route.arguments["name"]), usage=total)

            if not calls:
                self._remember(state, Message(role="assistant", content=text))
                return LoopResult("reply", text, usage=total)

            if iteration == self._max_iterations:
                self._remember(state, Message(role="assistant", content=text))
                return LoopResult(
                    "handoff", text, handoff_reason=HANDOFF_TOOL_LOOP_LIMIT, usage=total
                )

            self._remember(state, Message(role="assistant", content=text, tool_calls=tuple(calls)))
            await self._run_tools(calls, state, timer)

        raise AssertionError("unreachable: the last iteration always returns")

    async def _call_llm(
        self,
        messages: list[Message],
        specs: list[ToolSpec],
        state: SessionState,
        timer: TurnTimer,
    ) -> tuple[str, list[ToolCall], Usage | None]:
        """One streamed LLM call: forwards text, collects tool calls and usage."""
        parts: list[str] = []
        calls: list[ToolCall] = []
        usage: Usage | None = None
        try:
            with timer.stage("llm_total"):
                stream = self._provider.stream(messages, specs, model=self._model)
                async with aclosing(stream) as events:
                    event = await self._first_event(events, timer)
                    while event is not None:
                        match event:
                            case TextDelta(text=chunk):
                                parts.append(chunk)
                                timer.first_text()
                                await self._emit(
                                    protocol.AssistantDelta(turn_id=state.turn, text=chunk)
                                )
                            case ToolCall():
                                calls.append(event)
                            case Usage():
                                usage = event
                        event = await anext(events, None)
        except asyncio.CancelledError:
            # The subscriber interrupted: keep what they heard, marked as cut off.
            if parts:
                partial = Message(role="assistant", content="".join(parts))
                entry = HistoryEntry(turn=state.turn, message=partial, interrupted=True)
                state.history.append(entry)
            raise
        return "".join(parts), calls, usage

    async def _first_event(
        self, events: AsyncGenerator[StreamEvent], timer: TurnTimer
    ) -> StreamEvent | None:
        """Wait for the first event within the first-token budget; summed as ``llm_ttft``."""
        try:
            with timer.stage("llm_ttft"):
                async with asyncio.timeout(self._first_token_timeout):
                    return await anext(events, None)
        except TimeoutError:
            raise ProviderError(
                f"no first token within {self._first_token_timeout:g} s", retryable=True
            ) from None

    async def _run_tools(
        self, calls: Sequence[ToolCall], state: SessionState, timer: TurnTimer
    ) -> None:
        """Run the calls in order and record a result for every one, even on cancel."""
        interrupted = False
        for call in calls:
            finished_on_cancel = False
            if interrupted:
                result = tool_error("cancelled", "The subscriber interrupted; not run.")
            else:
                try:
                    result, interrupted = await self._run_tool(call, state, timer)
                    finished_on_cancel = interrupted
                except asyncio.CancelledError:
                    result = tool_error("cancelled", "The subscriber interrupted; not run.")
                    interrupted = True
            content = json.dumps(result, ensure_ascii=False)
            self._remember(state, Message(role="tool", content=content, tool_call_id=call.id))
            if finished_on_cancel:
                # The side effect happened: the call log must show it. The result is already
                # in the history, so a second cancellation here loses nothing; it is raised
                # below with the first one.
                with contextlib.suppress(asyncio.CancelledError):
                    await self._emit(
                        protocol.ToolResult(turn_id=state.turn, name=call.name, result=result)
                    )
        if interrupted:
            raise asyncio.CancelledError

    async def _run_tool(
        self, call: ToolCall, state: SessionState, timer: TurnTimer
    ) -> tuple[ToolResult, bool]:
        """One call; returns the result and whether a cancellation arrived while it ran.

        Only side-effect tools report a cancellation: they are finished under ``shield``.
        Other tools are simply cancelled, and the caller records them as ``cancelled``.
        """
        await self._emit(protocol.ToolCall(turn_id=state.turn, name=call.name, args=call.arguments))
        interrupted = False
        if call.name == ROUTE_TOOL:
            # A route name the node does not have; the LLM gets a chance to fix it.
            result = tool_error("unknown_route", "This route does not exist here.")
        else:
            with timer.stage(f"tool.{call.name}"):
                work = call_tool(self._registry, call, self._ctx, state)
                if self._registry.has_side_effect(call.name):
                    result, interrupted = await complete_despite_cancel(work)
                else:
                    result = await work
        if interrupted:
            # No await before the caller records the result: a second cancellation must not
            # lose it. The caller emits the ``tool_result`` after recording.
            return result, True
        await self._emit(protocol.ToolResult(turn_id=state.turn, name=call.name, result=result))
        if result.get("error") in _FAILED:
            await self._emit(
                protocol.Error(
                    code="tool_failed",
                    message=f"The action {call.name!r} failed; the assistant was told.",
                    retryable=True,
                )
            )
        return result, False

    @staticmethod
    def _remember(state: SessionState, message: Message) -> None:
        if message.content or message.tool_calls or message.role == "tool":
            state.history.append(HistoryEntry(turn=state.turn, message=message))
