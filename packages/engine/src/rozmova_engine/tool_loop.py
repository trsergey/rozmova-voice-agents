"""The function-calling loop of one ``llm_prompt`` step (SDD "LLM-шар").

LLM → tool calls → results back to the LLM → … until the model answers with text, asks
for a route, or runs out of iterations. Text is forwarded through ``emit`` as soon as it
arrives; tool calls go through ``call_tool``, so two-phase tools keep their confirmation
rule. Everything the step adds to the conversation is appended to ``state.history``.

Limits:

- at most ``max_iterations`` LLM calls (4); if the last one still asks for tools, those
  calls are not executed (the model would never see their results) and the loop ends with
  a handoff, which the executor turns into a ``handoff`` event;
- every LLM call must yield its first event within ``first_token_timeout`` (10 s).
"""

import asyncio
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

Emit = Callable[[protocol.ProtocolMessage], Awaitable[None]]

MAX_ITERATIONS = 4
FIRST_TOKEN_TIMEOUT = 10.0

# The service tool through which the LLM leaves the node along a `when.route` edge.
ROUTE_TOOL = "route"
HANDOFF_TOOL_LOOP_LIMIT = "tool_loop_limit"


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
            text, calls, usage = await self._call_llm(messages, specs, state.turn, timer)
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
            for call in calls:
                result = await self._run_tool(call, state, timer)
                content = json.dumps(result, ensure_ascii=False)
                self._remember(state, Message(role="tool", content=content, tool_call_id=call.id))

        raise AssertionError("unreachable: the last iteration always returns")

    async def _call_llm(
        self, messages: list[Message], specs: list[ToolSpec], turn: int, timer: TurnTimer
    ) -> tuple[str, list[ToolCall], Usage | None]:
        """One streamed LLM call: forwards text, collects tool calls and usage."""
        parts: list[str] = []
        calls: list[ToolCall] = []
        usage: Usage | None = None
        with timer.stage("llm_total"):
            stream = self._provider.stream(messages, specs, model=self._model)
            async with aclosing(stream) as events:
                event = await self._first_event(events, timer)
                while event is not None:
                    match event:
                        case TextDelta(text=chunk):
                            parts.append(chunk)
                            timer.first_text()
                            await self._emit(protocol.AssistantDelta(turn_id=turn, text=chunk))
                        case ToolCall():
                            calls.append(event)
                        case Usage():
                            usage = event
                    event = await anext(events, None)
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

    async def _run_tool(self, call: ToolCall, state: SessionState, timer: TurnTimer) -> ToolResult:
        await self._emit(protocol.ToolCall(turn_id=state.turn, name=call.name, args=call.arguments))
        if call.name == ROUTE_TOOL:
            # A route name the node does not have; the LLM gets a chance to fix it.
            result = tool_error("unknown_route", "This route does not exist here.")
        else:
            with timer.stage(f"tool.{call.name}"):
                result = await call_tool(self._registry, call, self._ctx, state)
        await self._emit(protocol.ToolResult(turn_id=state.turn, name=call.name, result=result))
        return result

    @staticmethod
    def _remember(state: SessionState, message: Message) -> None:
        if message.content or message.tool_calls or message.role == "tool":
            state.history.append(HistoryEntry(turn=state.turn, message=message))
