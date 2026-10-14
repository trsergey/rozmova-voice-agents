"""Tool registry: Pydantic argument models, JSON Schema for the LLM, guarded execution.

A tool is a name, a description for the model, an argument model and an async handler.
The argument model is the single source for both the JSON Schema sent to the LLM and the
validation of what the LLM sends back (SDD "LLM-шар"). The registry never raises for a bad
call: unknown tools, invalid arguments, timeouts and handler failures come back as a
structured ``{"error": ...}`` result the LLM can react to.

A tool with a ``quote`` is two-phase (ADR 0013): ``tools.confirmation.call_tool`` quotes it
first and executes it only on a repeated call in a later turn.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

import structlog
from pydantic import BaseModel, JsonValue, ValidationError

from rozmova_engine.providers import ToolCall, ToolSpec

log = structlog.get_logger(__name__)

ToolResult = dict[str, JsonValue]

# SDD "Діалоговий гейтвей": every tool call is bounded by 5 s.
DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a handler may know about the call it serves."""

    subscriber_id: str


@dataclass(frozen=True, slots=True)
class Quote:
    """What the subscriber must confirm, e.g. ``{"service": ..., "price": ...}``."""

    details: ToolResult


@dataclass(frozen=True, slots=True)
class Tool[A: BaseModel]:
    """One action the LLM can call.

    ``side_effect`` marks tools that change subscriber data; the gateway runs them under
    ``asyncio.shield()`` so a barge-in cannot cancel them halfway. ``quote`` makes the tool
    two-phase: it returns a ``Quote`` to confirm, or a final result when there is nothing to
    confirm (unknown or already active service).
    """

    name: str
    description: str
    args_model: type[A]
    handler: Callable[[A, ToolContext], Awaitable[ToolResult]]
    side_effect: bool = False
    quote: Callable[[A, ToolContext], Awaitable[Quote | ToolResult]] | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )


@dataclass(frozen=True, slots=True)
class BoundCall:
    """A call whose tool exists and whose arguments passed the tool's model."""

    tool: Tool[Any]
    args: BaseModel

    def normalized_args(self) -> dict[str, JsonValue]:
        """Arguments as validated JSON, so equal calls compare equal."""
        return cast(dict[str, JsonValue], self.args.model_dump(mode="json"))


class UnknownToolError(LookupError):
    """A flow or caller asked for a tool that is not registered."""


def tool_error(code: str, message: str, **details: JsonValue) -> ToolResult:
    """The result shape for every failed call, from the registry or from a handler."""
    return {"error": code, "message": message, **details}


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool[Any]] = (), *, timeout: float = DEFAULT_TIMEOUT):
        self._tools: dict[str, Tool[Any]] = {}
        self._timeout = timeout
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool {name!r}") from None

    def has_side_effect(self, name: str) -> bool:
        """Whether ``name`` changes subscriber data; false for unknown tools."""
        tool = self._tools.get(name)
        return tool is not None and tool.side_effect

    def specs(self, names: Iterable[str]) -> list[ToolSpec]:
        """Specs for the tools a node allows, in the node's order."""
        return [self.get(name).spec for name in names]

    def bind(self, call: ToolCall) -> BoundCall | ToolResult:
        """Resolve the tool and validate the arguments, or explain to the LLM what is wrong."""
        tool = self._tools.get(call.name)
        if tool is None:
            return tool_error("unknown_tool", f"Tool {call.name!r} does not exist.")
        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            details: list[JsonValue] = [
                {"field": ".".join(str(part) for part in err["loc"]), "problem": err["msg"]}
                for err in exc.errors()
            ]
            return tool_error("invalid_arguments", "Fix the arguments and retry.", details=details)
        return BoundCall(tool, args)

    async def call(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Validate and execute a single-phase tool in one step.

        Two-phase tools are refused, so the confirmation rule cannot be skipped by accident;
        they go through ``tools.confirmation.call_tool``.
        """
        bound = self.bind(call)
        if not isinstance(bound, BoundCall):
            return bound
        if bound.tool.quote is not None:
            raise ValueError(f"tool {bound.tool.name!r} is two-phase, use call_tool")
        return await self.execute(bound, ctx)

    async def execute(self, bound: BoundCall, ctx: ToolContext) -> ToolResult:
        """Run the handler within the timeout."""
        handler = bound.tool.handler
        return await self._guarded(bound.tool.name, lambda: handler(bound.args, ctx))

    async def quote(self, bound: BoundCall, ctx: ToolContext) -> Quote | ToolResult:
        """Run the tool's quote within the timeout; the tool must be two-phase."""
        quote = bound.tool.quote
        if quote is None:
            raise ValueError(f"tool {bound.tool.name!r} is not two-phase")
        return await self._guarded(bound.tool.name, lambda: quote(bound.args, ctx))

    async def _guarded[T](self, name: str, run: Callable[[], Awaitable[T]]) -> T | ToolResult:
        """Bound ``run`` by the timeout and turn failures into results.

        Cancellation is not caught: it propagates to the turn that owns the call.
        """
        try:
            async with asyncio.timeout(self._timeout):
                return await run()
        except TimeoutError:
            log.warning("tool_timeout", tool=name, timeout_s=self._timeout)
            return tool_error("timeout", f"Tool {name!r} did not answer in time.")
        except Exception:
            log.exception("tool_failed", tool=name)
            return tool_error("tool_failed", f"Tool {name!r} failed.")
