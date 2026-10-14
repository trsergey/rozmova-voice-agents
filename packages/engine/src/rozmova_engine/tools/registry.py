"""Tool registry: Pydantic argument models, JSON Schema for the LLM, guarded execution.

A tool is a name, a description for the model, an argument model and an async handler.
The argument model is the single source for both the JSON Schema sent to the LLM and the
validation of what the LLM sends back (SDD "LLM-шар"). ``ToolRegistry.call`` never raises
for a bad call: unknown tools, invalid arguments, timeouts and handler failures come back
as a structured ``{"error": ...}`` result the LLM can react to.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

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
class Tool[A: BaseModel]:
    """One action the LLM can call.

    ``side_effect`` marks tools that change subscriber data; the gateway runs them under
    ``asyncio.shield()`` so a barge-in cannot cancel them halfway.
    """

    name: str
    description: str
    args_model: type[A]
    handler: Callable[[A, ToolContext], Awaitable[ToolResult]]
    side_effect: bool = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )


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

    def specs(self, names: Iterable[str]) -> list[ToolSpec]:
        """Specs for the tools a node allows, in the node's order."""
        return [self.get(name).spec for name in names]

    async def call(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Validate the arguments and run the handler within the timeout.

        Cancellation is not caught: it propagates to the turn that owns the call.
        """
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

        try:
            async with asyncio.timeout(self._timeout):
                return await tool.handler(args, ctx)
        except TimeoutError:
            log.warning("tool_timeout", tool=tool.name, timeout_s=self._timeout)
            return tool_error("timeout", f"Tool {tool.name!r} did not answer in time.")
        except Exception:
            log.exception("tool_failed", tool=tool.name)
            return tool_error("tool_failed", f"Tool {tool.name!r} failed.")
