"""OpenAICompatibleProvider against recorded SSE streams served by an httpx2 mock transport."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from rozmova_engine.providers import (
    Message,
    OpenAICompatibleProvider,
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
from rozmova_engine.providers.openai_compat import (
    OPENROUTER_BASE_URL,
    azure_openai_provider,
    openai_provider,
    openrouter_provider,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openai"
USER = Message(role="user", content="Який у мене баланс?")
BALANCE_TOOL = ToolSpec(
    name="get_balance",
    description="Баланс абонента",
    parameters={"type": "object", "properties": {"msisdn": {"type": "string"}}},
)

Handler = Callable[[httpx2.Request], httpx2.Response]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def sse(body: bytes | httpx2.AsyncByteStream, status: int = 200) -> httpx2.Response:
    headers = {"content-type": "text/event-stream"}
    if isinstance(body, bytes):
        return httpx2.Response(status, headers=headers, content=body)
    return httpx2.Response(status, headers=headers, stream=body)


def error(status: int) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": "nope", "type": "server_error"}})


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


def make_provider(server: Server, sleeps: Sleeps | None = None) -> OpenAICompatibleProvider:
    client = AsyncOpenAI(api_key="sk-test", max_retries=0, http_client=server.client())
    return OpenAICompatibleProvider(client, retry=RetryPolicy(sleep=sleeps or Sleeps()))


async def collect(
    provider: OpenAICompatibleProvider,
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
    received: list[StreamEvent] | None = None,
) -> list[StreamEvent]:
    events = received if received is not None else []
    stream = provider.stream(messages or [USER], tools or [], model="gpt-4.1-mini")
    async with aclosing(stream) as events_stream:
        async for event in events_stream:
            events.append(event)
    return events


# --- streaming ---------------------------------------------------------------------


async def test_text_stream_maps_to_deltas_then_usage() -> None:
    server = Server(sse(fixture("text.sse")))
    events = await collect(make_provider(server))
    assert events == [
        TextDelta(text="Ваш баланс"),
        TextDelta(text=" — 120 грн."),
        Usage(input_tokens=42, output_tokens=7),
    ]
    body = server.body()
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "gpt-4.1-mini"
    assert "tools" not in body  # an empty tools list is rejected by the API


async def test_tool_call_fragments_are_assembled_per_call() -> None:
    server = Server(sse(fixture("tool_calls.sse")))
    events = await collect(make_provider(server), tools=[BALANCE_TOOL])
    assert events == [
        ToolCall(id="call_bal", name="get_balance", arguments={"msisdn": "+380671234567"}),
        ToolCall(id="call_tariff", name="get_tariff", arguments={}),
        Usage(input_tokens=120, output_tokens=31),
    ]
    assert server.body()["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_balance",
                "description": "Баланс абонента",
                "parameters": BALANCE_TOOL.parameters,
            },
        }
    ]


async def test_conversation_with_tool_round_trip_is_sent_in_openai_format() -> None:
    call = ToolCall(id="call_bal", name="get_balance", arguments={"msisdn": "+380671234567"})
    messages = [
        Message(role="system", content="Ти помічник оператора."),
        USER,
        Message(role="assistant", tool_calls=(call,)),
        Message(role="tool", tool_call_id="call_bal", content='{"balance": 120}'),
    ]
    server = Server(sse(fixture("text.sse")))
    await collect(make_provider(server), messages=messages)
    assert server.body()["messages"] == [
        {"role": "system", "content": "Ти помічник оператора."},
        {"role": "user", "content": "Який у мене баланс?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_bal",
                    "type": "function",
                    "function": {"name": "get_balance", "arguments": '{"msisdn": "+380671234567"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_bal", "content": '{"balance": 120}'},
    ]


async def test_malformed_tool_arguments_fail_attempt_and_are_retried() -> None:
    server = Server(sse(fixture("malformed_tool_args.sse")), sse(fixture("tool_calls.sse")))
    sleeps = Sleeps()
    events = await collect(make_provider(server, sleeps), tools=[BALANCE_TOOL])
    assert isinstance(events[0], ToolCall)
    assert len(server.requests) == 2
    assert sleeps.delays == [0.5]


async def test_malformed_tool_arguments_raise_retryable_error_when_attempts_run_out() -> None:
    server = Server(*(sse(fixture("malformed_tool_args.sse")) for _ in range(3)))
    with pytest.raises(ProviderError, match="malformed JSON arguments") as info:
        await collect(make_provider(server), tools=[BALANCE_TOOL])
    assert info.value.retryable is True


# --- retries -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_status_is_retried_with_exponential_backoff(status: int) -> None:
    server = Server(error(status), error(status), sse(fixture("text.sse")))
    sleeps = Sleeps()
    events = await collect(make_provider(server, sleeps))
    assert events[-1] == Usage(input_tokens=42, output_tokens=7)
    assert len(server.requests) == 3
    assert sleeps.delays == [0.5, 1.0]


async def test_three_failed_attempts_raise_retryable_error() -> None:
    server = Server(error(429), error(502), error(500))
    sleeps = Sleeps()
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server, sleeps))
    assert info.value.retryable is True
    assert "HTTP 500" in str(info.value)
    assert len(server.requests) == 3
    assert sleeps.delays == [0.5, 1.0]


async def test_timeout_and_connection_errors_are_retried() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    server = Server(
        httpx2.ReadTimeout("slow", request=request),
        httpx2.ConnectError("down", request=request),
        sse(fixture("text.sse")),
    )
    events = await collect(make_provider(server))
    assert TextDelta(text="Ваш баланс") in events
    assert len(server.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_client_errors_are_not_retried(status: int) -> None:
    server = Server(error(status))
    sleeps = Sleeps()
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server, sleeps))
    assert info.value.retryable is False
    assert len(server.requests) == 1
    assert sleeps.delays == []


class BrokenAfterFirstChunk(httpx2.AsyncByteStream):
    """Sends the first SSE event of a fixture, then the connection drops."""

    def __init__(self, body: bytes) -> None:
        self.first = body.split(b"\n\n")[1] + b"\n\n"

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx2.ReadError("connection reset")


async def test_failure_after_first_event_is_not_retried() -> None:
    server = Server(sse(BrokenAfterFirstChunk(fixture("text.sse"))), sse(fixture("text.sse")))
    sleeps = Sleeps()
    received: list[StreamEvent] = []
    with pytest.raises(ProviderError) as info:
        await collect(make_provider(server, sleeps), received=received)
    assert received == [TextDelta(text="Ваш баланс")]
    assert info.value.retryable is True
    assert len(server.requests) == 1
    assert sleeps.delays == []


def test_backoff_delays_are_capped() -> None:
    policy = RetryPolicy(attempts=6, base_delay=0.5, max_delay=2.0)
    assert [policy.delay(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 2.0, 2.0]
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=0)


# --- cancellation ------------------------------------------------------------------


class Stalls(httpx2.AsyncByteStream):
    """Sends one SSE event, then waits forever; records whether it was closed."""

    def __init__(self, body: bytes) -> None:
        self.first = body.split(b"\n\n")[1] + b"\n\n"
        self.stalled = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        self.stalled.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_cancelling_the_turn_closes_the_http_stream() -> None:
    body = Stalls(fixture("text.sse"))
    server = Server(sse(body))
    received: list[StreamEvent] = []
    task = asyncio.create_task(collect(make_provider(server), received=received))
    async with asyncio.timeout(1):
        await body.stalled.wait()
    assert received == [TextDelta(text="Ваш баланс")]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed


# --- factories ---------------------------------------------------------------------


def test_adapters_are_registered_under_bot_provider_names() -> None:
    assert {"openai", "azure_openai", "openrouter"} <= registry.names
    provider = registry.create("openai", ProviderConfig(api_key=SecretStr("sk-test")))
    assert isinstance(provider, OpenAICompatibleProvider)


async def test_openai_factory_honours_base_url() -> None:
    server = Server(sse(fixture("text.sse")))
    config = ProviderConfig(api_key=SecretStr("sk-test"), base_url="https://llm.example/v1")
    await collect(openai_provider(config, http_client=server.client()))
    request = server.requests[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"


async def test_openrouter_factory_defaults_to_openrouter_endpoint() -> None:
    server = Server(sse(fixture("text.sse")))
    config = ProviderConfig(api_key=SecretStr("sk-or-test"))
    await collect(openrouter_provider(config, http_client=server.client()))
    assert str(server.requests[0].url) == f"{OPENROUTER_BASE_URL}/chat/completions"


# Azure sends a content-filter chunk with no choices before the answer.
AZURE_PROMPT_FILTER = (
    b'data: {"choices":[],"created":0,"id":"","model":"","object":"",'
    b'"prompt_filter_results":[{"prompt_index":0,"content_filter_results":{}}]}\n\n'
)


async def test_azure_factory_routes_model_to_deployment() -> None:
    server = Server(sse(AZURE_PROMPT_FILTER + fixture("text.sse")))
    config = ProviderConfig(
        api_key=SecretStr("azure-key"),
        base_url="https://rozmova.openai.azure.com",
        api_version="2024-10-21",
    )
    provider = azure_openai_provider(config, http_client=server.client())
    stream = provider.stream([USER], [], model="gpt41mini-prod")
    async with aclosing(stream) as events_stream:
        events = [event async for event in events_stream]
    assert events[0] == TextDelta(text="Ваш баланс")
    request = server.requests[0]
    assert request.url.path == "/openai/deployments/gpt41mini-prod/chat/completions"
    assert request.url.params["api-version"] == "2024-10-21"
    assert request.headers["api-key"] == "azure-key"


def test_azure_factory_requires_endpoint_and_api_version() -> None:
    with pytest.raises(ValueError, match="api_version"):
        azure_openai_provider(ProviderConfig(api_key=SecretStr("k"), base_url="https://x"))


async def test_aclose_closes_the_sdk_client() -> None:
    client = AsyncOpenAI(api_key="sk-test", http_client=Server().client())
    await OpenAICompatibleProvider(client).aclose()
    assert client.is_closed()
