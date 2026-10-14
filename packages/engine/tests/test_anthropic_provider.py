"""AnthropicProvider against recorded SSE streams served by an httpx2 mock transport."""

import json
from contextlib import aclosing
from pathlib import Path

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic import SecretStr

from rozmova_engine.providers import (
    AnthropicProvider,
    Message,
    ProviderConfig,
    ProviderError,
    RetryPolicy,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    registry,
)

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"
MODEL = "claude-haiku-5-5"
USER = Message(role="user", content="Який у мене баланс?")
ACTIVATE_TOOL = ToolSpec(
    name="activate_service",
    description="Підключає послугу",
    parameters={"type": "object", "properties": {"service_id": {"type": "string"}}},
)


def sse(name: str) -> httpx2.Response:
    # An SSE event ends with a blank line; the end-of-file hook trims the last one.
    content = (FIXTURES / name).read_bytes().rstrip(b"\n") + b"\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content)


def error(status: int) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": "api_error", "message": "nope"}}
    )


class Server:
    """Replays queued responses and records the requests it received."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def body(self, number: int = 0) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.requests[number].content)
        return parsed


class Sleeps:
    """Records backoff delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_provider(server: Server, sleeps: Sleeps | None = None) -> AnthropicProvider:
    client = AsyncAnthropic(api_key="sk-ant-test", max_retries=0, http_client=server.client())
    return AnthropicProvider(client, retry=RetryPolicy(sleep=sleeps or Sleeps()))


async def collect(
    provider: AnthropicProvider,
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
    received: list[StreamEvent] | None = None,
) -> list[StreamEvent]:
    events = received if received is not None else []
    async with aclosing(provider.stream(messages or [USER], tools or [], model=MODEL)) as stream:
        async for event in stream:
            events.append(event)
    return events


# --- streaming ---------------------------------------------------------------------


async def test_text_stream_maps_to_deltas_then_usage() -> None:
    server = Server(sse("text.sse"))
    events = await collect(make_provider(server))

    assert events == [
        TextDelta(text="Ваш баланс"),
        TextDelta(text=" — 120,50 грн."),
        # 40 uncached + 2 cache-read input tokens.
        Usage(input_tokens=42, output_tokens=9),
    ]
    body = server.body()
    assert body["model"] == MODEL
    assert body["stream"] is True
    assert body["max_tokens"] == 1024
    assert body["thinking"] == {"type": "disabled"}
    assert "tools" not in body
    assert "system" not in body


async def test_tool_use_blocks_become_tool_calls() -> None:
    server = Server(sse("tool_use.sse"))
    events = await collect(make_provider(server), tools=[ACTIVATE_TOOL])

    assert events == [
        TextDelta(text="Зараз перевірю."),
        ToolCall(id="toolu_1", name="activate_service", arguments={"service_id": "roaming_eu"}),
        ToolCall(id="toolu_2", name="get_balance", arguments={}),
        Usage(input_tokens=120, output_tokens=31),
    ]
    assert server.body()["tools"] == [
        {
            "name": "activate_service",
            "description": "Підключає послугу",
            "input_schema": ACTIVATE_TOOL.parameters,
        }
    ]


async def test_history_is_sent_in_anthropic_format() -> None:
    balance = ToolCall(id="toolu_1", name="get_balance", arguments={})
    tariff = ToolCall(id="toolu_2", name="get_current_tariff", arguments={})
    messages = [
        Message(role="system", content="Ти асистент оператора Hvylia Mobile."),
        Message(role="system", content="Відповідай коротко."),
        USER,
        Message(role="assistant", content="Перевіряю.", tool_calls=(balance, tariff)),
        Message(role="tool", tool_call_id="toolu_1", content='{"balance": "120.50"}'),
        Message(role="tool", tool_call_id="toolu_2", content='{"name": "Оптимум"}'),
        Message(role="assistant", content="На рахунку 120,50 грн."),
        Message(role="assistant"),  # empty, e.g. a reply interrupted before any text
        Message(role="user", content="дякую"),
    ]
    server = Server(sse("text.sse"))
    await collect(make_provider(server), messages=messages)

    body = server.body()
    assert body["system"] == "Ти асистент оператора Hvylia Mobile.\n\nВідповідай коротко."
    assert body["messages"] == [
        {"role": "user", "content": "Який у мене баланс?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Перевіряю."},
                {"type": "tool_use", "id": "toolu_1", "name": "get_balance", "input": {}},
                {"type": "tool_use", "id": "toolu_2", "name": "get_current_tariff", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"balance": "120.50"}',
                },
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": '{"name": "Оптимум"}'},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "На рахунку 120,50 грн."}]},
        {"role": "user", "content": "дякую"},
    ]


async def test_malformed_tool_input_is_retried_before_the_first_event() -> None:
    server = Server(sse("malformed_tool_input.sse"), sse("tool_use.sse"))
    sleeps = Sleeps()
    events = await collect(make_provider(server, sleeps), tools=[ACTIVATE_TOOL])

    assert events[0] == TextDelta(text="Зараз перевірю.")
    assert len(server.requests) == 2
    assert sleeps.delays == [0.5]


async def test_tool_call_cut_by_max_tokens_is_not_retried() -> None:
    server = Server(sse("max_tokens_tool.sse"))
    with pytest.raises(ProviderError, match="max_tokens") as info:
        await collect(make_provider(server), tools=[ACTIVATE_TOOL])
    assert info.value.retryable is False
    assert len(server.requests) == 1


async def test_refusal_is_a_non_retryable_error() -> None:
    server = Server(sse("refusal.sse"))
    with pytest.raises(ProviderError, match="refused") as info:
        await collect(make_provider(server))
    assert info.value.retryable is False


# --- retries -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_retryable_status_is_retried_with_backoff(status: int) -> None:
    server = Server(error(status), error(status), sse("text.sse"))
    sleeps = Sleeps()
    events = await collect(make_provider(server, sleeps))

    assert events[0] == TextDelta(text="Ваш баланс")
    assert sleeps.delays == [0.5, 1.0]


async def test_retries_stop_after_three_attempts() -> None:
    server = Server(error(503), error(503), error(503))
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server))
    assert info.value.retryable is True
    assert len(server.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_client_errors_are_not_retried(status: int) -> None:
    server = Server(error(status))
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server))
    assert info.value.retryable is False
    assert len(server.requests) == 1


async def test_connection_error_is_retried() -> None:
    server = Server(httpx2.ConnectError("refused"), sse("text.sse"))
    events = await collect(make_provider(server))
    assert isinstance(events[-1], Usage)


async def test_overloaded_event_before_output_is_retried() -> None:
    server = Server(sse("overloaded.sse"), sse("text.sse"))
    events = await collect(make_provider(server))
    assert events[0] == TextDelta(text="Ваш баланс")
    assert len(server.requests) == 2


async def test_failure_after_first_event_is_raised_not_retried() -> None:
    server = Server(sse("overloaded_after_text.sse"), sse("text.sse"))
    received: list[StreamEvent] = []
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server), received=received)

    assert received == [TextDelta(text="Ваш")]
    assert info.value.retryable is True
    assert len(server.requests) == 1


# --- registry ----------------------------------------------------------------------


async def test_registered_factory_uses_config() -> None:
    server = Server(sse("text.sse"))
    provider = registry.create("anthropic", ProviderConfig(api_key=SecretStr("sk-ant-real")))
    assert isinstance(provider, AnthropicProvider)
    await provider.aclose()

    from rozmova_engine.providers.anthropic import anthropic_provider

    configured = anthropic_provider(
        ProviderConfig(api_key=SecretStr("sk-ant-real"), base_url="https://proxy.example/"),
        http_client=server.client(),
    )
    await collect(configured)
    request = server.requests[0]
    assert str(request.url) == "https://proxy.example/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-real"
