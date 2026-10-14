"""Provider-neutral LLM types: messages, tool specs, stream events and the provider protocol.

Code outside ``providers/`` talks to models only through these types, so a bot switches
provider in configuration (FR-16) without code changes. See ADR 0003 and the SDD section
"LLM-шар".
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_CONFIG = ConfigDict(extra="forbid", frozen=True)

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A complete tool call requested by the model.

    Adapters accumulate streamed argument fragments and emit one ``ToolCall`` per call
    once its arguments are parsed. ``id`` links the call to its ``tool`` result message.
    """

    model_config = _CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class TextDelta(BaseModel):
    """A fragment of assistant text, forwarded to the client as soon as it arrives."""

    model_config = _CONFIG

    text: str = Field(min_length=1)


class Usage(BaseModel):
    """Token counts for one ``stream()`` call; adapters emit it last."""

    model_config = _CONFIG

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


StreamEvent = TextDelta | ToolCall | Usage


class Message(BaseModel):
    """One entry of the conversation sent to the model.

    ``assistant`` messages may carry the tool calls the model made; each call is answered
    by a ``tool`` message with the matching ``tool_call_id`` and the result as ``content``.
    """

    model_config = _CONFIG

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_tool_fields(self) -> Self:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages carry tool_calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool_call_id is required for tool messages and only for them")
        return self


def _empty_object_schema() -> dict[str, JsonValue]:
    return {"type": "object", "properties": {}}


class ToolSpec(BaseModel):
    """A tool the model may call; ``parameters`` is the JSON Schema of its arguments."""

    model_config = _CONFIG

    # The strictest common rule across OpenAI and Anthropic tool names.
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, JsonValue] = Field(default_factory=_empty_object_schema)


class ProviderError(Exception):
    """A provider failure mapped from a vendor error.

    ``retryable`` is true for rate limits, 5xx and timeouts. Adapters retry on their own
    before the first event; once text has reached the client they raise instead, and the
    gateway turns this into an ``error`` event with the same flag.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    """Streams one model response as provider-neutral events.

    Implementations are async generators and the request starts on the first iteration.
    Callers iterate under ``async with contextlib.aclosing(...)``: cancelling the turn then
    closes the underlying HTTP stream instead of leaving it to garbage collection.
    """

    def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec], *, model: str
    ) -> AsyncGenerator[StreamEvent]: ...
