"""Adapter for the Anthropic Messages API (Claude), registered as ``anthropic``.

Maps the raw streaming events to the common ones and the common history to Anthropic's
shapes; retries go through the shared ``stream_with_retries`` like the OpenAI adapter.
Behaviour worth knowing:

- ``system`` messages are joined into the top-level ``system`` prompt; consecutive ``tool``
  messages become one user message with several ``tool_result`` blocks, as the API
  expects for parallel calls;
- a ``ToolCall`` is emitted when its ``tool_use`` block closes; ``Usage`` comes last, with
  cached prompt tokens counted as input;
- thinking is disabled by default: the bot needs a fast first token (SDD model criteria)
  and the common ``Message`` cannot carry thinking blocks, which the API expects back
  unchanged in a tool round. Models that reject ``{"type": "disabled"}`` (Claude Opus 5.5,
  Claude Sonnet 5.5) are therefore not supported in the MVP;
- a ``refusal`` or a ``max_tokens`` stop that cuts a tool call short fails the attempt
  with a non-retryable ``ProviderError``: the same request would end the same way.
"""

import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field

import httpx2
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncAnthropic,
    omit,
)
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ThinkingConfigParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from rozmova_engine.providers.base import (
    Message,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from rozmova_engine.providers.registry import ProviderConfig, registry
from rozmova_engine.providers.retry import RetryPolicy, stream_with_retries

# Same request bound as the OpenAI adapter; turn and first-token budgets are the caller's.
DEFAULT_TIMEOUT_S = 60.0
# Spoken replies are short; this caps a runaway answer, not a normal one.
DEFAULT_MAX_TOKENS = 1024

_RETRYABLE_STATUS = frozenset({408, 409, 429})
_RETRYABLE_STREAM_ERRORS = frozenset({"overloaded_error", "api_error", "rate_limit_error"})


class AnthropicProvider:
    """``LLMProvider`` over the official ``anthropic`` SDK client it is given."""

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: ThinkingConfigParam | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self._max_tokens = max_tokens
        self._thinking: ThinkingConfigParam = thinking or {"type": "disabled"}
        self._retry = retry or RetryPolicy()

    def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec], *, model: str
    ) -> AsyncGenerator[StreamEvent]:
        system, wire_messages = _to_wire_messages(messages)
        wire_tools = [_to_wire_tool(tool) for tool in tools]
        return stream_with_retries(
            lambda: self._attempt(system, wire_messages, wire_tools, model), self._retry
        )

    async def aclose(self) -> None:
        """Close the SDK client and its connection pool."""
        await self._client.close()

    async def _attempt(
        self,
        system: str,
        messages: list[MessageParam],
        tools: list[ToolParam],
        model: str,
    ) -> AsyncGenerator[StreamEvent]:
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                # Empty values are left out rather than sent as "" or [].
                system=system or omit,
                messages=messages,
                tools=tools or omit,
                thinking=self._thinking,
                stream=True,
            )
            async with response as events:
                blocks: dict[int, _PartialToolUse] = {}
                input_tokens = output_tokens = 0
                stop_reason: str | None = None
                async for event in events:
                    match event.type:
                        case "message_start":
                            usage = event.message.usage
                            input_tokens = (
                                usage.input_tokens
                                + (usage.cache_creation_input_tokens or 0)
                                + (usage.cache_read_input_tokens or 0)
                            )
                            output_tokens = usage.output_tokens
                        case "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                blocks[event.index] = _PartialToolUse(block.id, block.name)
                        case "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta" and delta.text:
                                yield TextDelta(text=delta.text)
                            elif delta.type == "input_json_delta" and event.index in blocks:
                                blocks[event.index].parts.append(delta.partial_json)
                        case "content_block_stop":
                            if event.index in blocks:
                                yield blocks.pop(event.index).finish()
                        case "message_delta":
                            stop_reason = event.delta.stop_reason or stop_reason
                            output_tokens = event.usage.output_tokens
                if stop_reason == "refusal":
                    raise ProviderError("the model refused to answer", retryable=False)
                if blocks:
                    # A tool_use block that never closed: the answer hit max_tokens.
                    raise ProviderError(
                        f"tool call cut off by stop reason {stop_reason!r}", retryable=False
                    )
                yield Usage(input_tokens=input_tokens, output_tokens=output_tokens)
        except AnthropicError as err:
            raise _to_provider_error(err) from err


@dataclass(slots=True)
class _PartialToolUse:
    """A ``tool_use`` block: id and name come in its start event, the input in pieces."""

    id: str
    name: str
    parts: list[str] = field(default_factory=list)

    def finish(self) -> ToolCall:
        raw = "".join(self.parts).strip() or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            raise ProviderError(f"tool call {self.name!r} has malformed JSON input", retryable=True)
        return ToolCall(id=self.id, name=self.name, arguments=arguments)


def _to_provider_error(err: AnthropicError) -> ProviderError:
    """Map an SDK error; the message names the class and status, never the request body."""
    name = type(err).__name__
    if isinstance(err, APIStatusError) and err.status_code == 200:
        # An `error` event inside a stream that started fine; its type says what happened.
        kind = getattr(err, "type", None)
        retryable = kind in _RETRYABLE_STREAM_ERRORS
        return ProviderError(f"Anthropic stream error {kind}", retryable=retryable)
    if isinstance(err, APIStatusError):
        status = err.status_code
        retryable = status in _RETRYABLE_STATUS or status >= 500
        return ProviderError(f"Anthropic API error {name} (HTTP {status})", retryable=retryable)
    # Connection failures and timeouts are transient; other SDK errors are not.
    retryable = isinstance(err, APIConnectionError) or type(err) is APIError
    return ProviderError(f"Anthropic API error {name}", retryable=retryable)


def _to_wire_messages(messages: Sequence[Message]) -> tuple[str, list[MessageParam]]:
    system: list[str] = []
    wire: list[MessageParam] = []
    for message in messages:
        match message.role:
            case "system":
                system.append(message.content)
            case "user":
                wire.append({"role": "user", "content": message.content})
            case "tool":
                result: ToolResultBlockParam = {
                    "type": "tool_result",
                    # Message validation guarantees the id on tool messages.
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
                previous = wire[-1] if wire else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                ):
                    previous["content"].append(result)
                else:
                    wire.append({"role": "user", "content": [result]})
            case "assistant":
                content: list[TextBlockParam | ToolUseBlockParam] = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    arguments: dict[str, object] = dict(call.arguments)
                    content.append(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": arguments}
                    )
                # The API rejects empty assistant turns; an empty one carries nothing anyway.
                if content:
                    wire.append({"role": "assistant", "content": content})
    return "\n\n".join(system), wire


def _to_wire_tool(tool: ToolSpec) -> ToolParam:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.parameters),
    }


# ``http_client`` is for tests (a mock transport); production uses the SDK default.
def anthropic_provider(
    config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None
) -> AnthropicProvider:
    """Anthropic API; ``base_url`` overrides the endpoint (e.g. a proxy)."""
    client = AsyncAnthropic(
        api_key=config.api_key.get_secret_value() if config.api_key else None,
        base_url=config.base_url,
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_S,
        http_client=http_client,
    )
    return AnthropicProvider(client)


registry.register("anthropic", anthropic_provider)
