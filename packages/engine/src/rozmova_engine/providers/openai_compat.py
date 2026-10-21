"""Adapter for OpenAI-compatible chat completions: OpenAI, Azure OpenAI and OpenRouter.

One class serves all three; they differ only in how the SDK client is built (SDD
"LLM-шар"). The SDK's own retries are off, ``stream_with_retries`` applies the shared
policy instead. Behaviour worth knowing:

- usage is requested in the stream (``stream_options.include_usage``) and emitted last;
  servers that do not send it produce no ``Usage`` event;
- tool-call fragments are assembled per call index and emitted as complete ``ToolCall``
  events after the text, once the stream has ended;
- GPT-5 and GPT-6 models get ``reasoning_effort="none"``: some reason by default, which
  costs first-token latency (ADR 0009); older models reject the parameter, and Azure
  deployment names do not match the prefixes, so they get the old request;
- tool-call arguments that are not a JSON object fail the attempt with
  ``ProviderError(retryable=True)``: before the first event that is retried like any
  transient failure, after it the gateway reports a retryable ``error``.
"""

import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    OpenAIError,
)
from openai.types.chat import (
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

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

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Upper bound for one HTTP request, including the whole stream. Turn and first-token
# budgets are tighter and enforced by the caller with ``asyncio.timeout``.
DEFAULT_TIMEOUT_S = 60.0

_RETRYABLE_STATUS = frozenset({408, 409, 429})
_REASONING_MODEL_PREFIXES = ("gpt-5", "gpt-6")


class OpenAICompatibleProvider:
    """``LLMProvider`` over the official ``openai`` SDK client it is given."""

    def __init__(self, client: AsyncOpenAI, *, retry: RetryPolicy | None = None) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()

    def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec], *, model: str
    ) -> AsyncGenerator[StreamEvent]:
        wire_messages = [_to_wire_message(message) for message in messages]
        wire_tools = [_to_wire_tool(tool) for tool in tools]
        return stream_with_retries(
            lambda: self._attempt(wire_messages, wire_tools, model), self._retry
        )

    async def aclose(self) -> None:
        """Close the SDK client and its connection pool."""
        await self._client.close()

    async def _attempt(
        self,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionFunctionToolParam],
        model: str,
    ) -> AsyncGenerator[StreamEvent]:
        try:
            # The API rejects an empty tools list, so it is only sent when non-empty.
            extra: dict[str, Any] = {"tools": tools} if tools else {}
            if model.startswith(_REASONING_MODEL_PREFIXES):
                extra["reasoning_effort"] = "none"
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **extra,
            )
            async with response as chunks:
                calls: dict[int, _PartialCall] = {}
                usage: Usage | None = None
                async for chunk in chunks:
                    if chunk.usage is not None:
                        usage = Usage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                        )
                    for choice in chunk.choices:
                        delta = choice.delta
                        if delta.content:
                            yield TextDelta(text=delta.content)
                        for fragment in delta.tool_calls or ():
                            calls.setdefault(fragment.index, _PartialCall()).add(fragment)
                for index in sorted(calls):
                    yield calls[index].finish(index)
                if usage is not None:
                    yield usage
        except OpenAIError as err:
            raise _to_provider_error(err) from err


@dataclass(slots=True)
class _PartialCall:
    """Fragments of one streamed tool call: id and name come first, arguments in pieces."""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def add(self, fragment: ChoiceDeltaToolCall) -> None:
        if fragment.id:
            self.id = fragment.id
        if fragment.function is not None:
            # The name arrives whole in the first fragment; some servers repeat it.
            if fragment.function.name and not self.name:
                self.name = fragment.function.name
            if fragment.function.arguments:
                self.arguments.append(fragment.function.arguments)

    def finish(self, index: int) -> ToolCall:
        if not self.name:
            raise ProviderError(f"tool call #{index} has no function name", retryable=True)
        raw = "".join(self.arguments).strip() or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            raise ProviderError(
                f"tool call {self.name!r} has malformed JSON arguments", retryable=True
            )
        # Some OpenAI-compatible servers omit ids; the id only links our own messages.
        return ToolCall(id=self.id or f"call_{index}", name=self.name, arguments=arguments)


def _to_provider_error(err: OpenAIError) -> ProviderError:
    """Map an SDK error; the message names the class and status, never the request body."""
    name = type(err).__name__
    if isinstance(err, APIStatusError):
        status = err.status_code
        retryable = status in _RETRYABLE_STATUS or status >= 500
        message = f"OpenAI-compatible API error {name} (HTTP {status})"
        return ProviderError(message, retryable=retryable)
    # Connection failures, timeouts and errors sent inside the stream are transient;
    # other SDK errors (e.g. response validation) are not.
    retryable = isinstance(err, APIConnectionError) or type(err) is APIError
    return ProviderError(f"OpenAI-compatible API error {name}", retryable=retryable)


def _to_wire_message(message: Message) -> ChatCompletionMessageParam:
    match message.role:
        case "system":
            return {"role": "system", "content": message.content}
        case "user":
            return {"role": "user", "content": message.content}
        case "tool":
            return {
                "role": "tool",
                # Message validation guarantees the id on tool messages.
                "tool_call_id": message.tool_call_id or "",
                "content": message.content,
            }
        case "assistant":
            if not message.tool_calls:
                return {"role": "assistant", "content": message.content}
            calls: list[ChatCompletionMessageFunctionToolCallParam] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": calls,
            }


def _to_wire_tool(tool: ToolSpec) -> ChatCompletionFunctionToolParam:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


# --- factories registered under ``bots.provider`` names ------------------------------
# ``http_client`` is for tests (a mock transport); production uses the SDK default.


def _api_key(config: ProviderConfig) -> str | None:
    return config.api_key.get_secret_value() if config.api_key else None


def openai_provider(
    config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None
) -> OpenAICompatibleProvider:
    """OpenAI, or any OpenAI-compatible server when ``base_url`` is set."""
    client = AsyncOpenAI(
        api_key=_api_key(config),
        base_url=config.base_url,
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_S,
        http_client=http_client,
    )
    return OpenAICompatibleProvider(client)


def openrouter_provider(
    config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None
) -> OpenAICompatibleProvider:
    """OpenRouter; ``model`` is the OpenRouter slug, e.g. ``openai/gpt-4.1-mini``."""
    client = AsyncOpenAI(
        api_key=_api_key(config),
        base_url=config.base_url or OPENROUTER_BASE_URL,
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_S,
        http_client=http_client,
    )
    return OpenAICompatibleProvider(client)


def azure_openai_provider(
    config: ProviderConfig, *, http_client: httpx2.AsyncClient | None = None
) -> OpenAICompatibleProvider:
    """Azure OpenAI: ``base_url`` is the resource endpoint, ``model`` the deployment name."""
    if not config.base_url or not config.api_version:
        raise ValueError("azure_openai needs base_url (resource endpoint) and api_version")
    client = AsyncAzureOpenAI(
        azure_endpoint=config.base_url,
        api_version=config.api_version,
        api_key=_api_key(config),
        max_retries=0,
        timeout=DEFAULT_TIMEOUT_S,
        http_client=http_client,
    )
    return OpenAICompatibleProvider(client)


registry.register("openai", openai_provider)
registry.register("openrouter", openrouter_provider)
registry.register("azure_openai", azure_openai_provider)
