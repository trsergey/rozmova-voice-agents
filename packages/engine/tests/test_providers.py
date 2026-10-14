import asyncio
from contextlib import aclosing

import pytest
from pydantic import SecretStr, ValidationError

from rozmova_engine.providers import (
    LLMProvider,
    Message,
    ProviderConfig,
    ProviderError,
    ProviderRegistry,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    UnknownProviderError,
    Usage,
)
from rozmova_engine.providers.fake import (
    Delay,
    FakeProvider,
    FakeScriptExhaustedError,
    Raise,
    text_turn,
)

USER = Message(role="user", content="Який у мене баланс?")
BALANCE_TOOL = ToolSpec(name="get_balance", description="Баланс абонента")


async def collect(provider: LLMProvider, messages: list[Message]) -> list[StreamEvent]:
    async with aclosing(provider.stream(messages, [BALANCE_TOOL], model="m")) as stream:
        return [event async for event in stream]


async def consume_into(provider: LLMProvider, received: list[StreamEvent]) -> None:
    """Like ``collect``, but keeps the events that arrived before a failure."""
    async with aclosing(provider.stream([USER], [], model="m")) as stream:
        async for event in stream:
            received.append(event)


# --- models ----------------------------------------------------------------------


def test_tool_message_requires_tool_call_id() -> None:
    with pytest.raises(ValidationError):
        Message(role="tool", content="{}")
    with pytest.raises(ValidationError):
        Message(role="user", content="hi", tool_call_id="c1")
    assert Message(role="tool", content="{}", tool_call_id="c1").tool_call_id == "c1"


def test_only_assistant_carries_tool_calls() -> None:
    call = ToolCall(id="c1", name="get_balance")
    with pytest.raises(ValidationError):
        Message(role="user", tool_calls=(call,))
    assert Message(role="assistant", tool_calls=(call,)).tool_calls == (call,)


def test_tool_spec_name_must_be_portable() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="get balance", description="")
    assert BALANCE_TOOL.parameters == {"type": "object", "properties": {}}


def test_events_are_immutable() -> None:
    delta = TextDelta(text="Привіт")
    with pytest.raises(ValidationError):
        delta.text = "x"


# --- FakeProvider ----------------------------------------------------------------


async def test_plays_text_turn_in_chunks_with_usage() -> None:
    usage = Usage(input_tokens=12, output_tokens=3)
    provider = FakeProvider([text_turn("Ваш баланс 120 грн.", chunk_size=5, usage=usage)])

    events = await collect(provider, [USER])

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert "".join(deltas) == "Ваш баланс 120 грн."
    assert all(len(d) <= 5 for d in deltas)
    assert events[-1] == usage


async def test_plays_turns_in_order_and_records_calls() -> None:
    call = ToolCall(id="c1", name="get_balance", arguments={})
    provider = FakeProvider([[call], text_turn("120 грн.")])

    first = await collect(provider, [USER])
    history = [
        USER,
        Message(role="assistant", tool_calls=(call,)),
        Message(role="tool", content='{"balance": 120}', tool_call_id="c1"),
    ]
    second = await collect(provider, history)

    assert first == [call]
    assert second == [TextDelta(text="120 грн.")]
    assert provider.remaining_turns == 0
    assert [c.messages for c in provider.calls] == [(USER,), tuple(history)]
    assert provider.calls[0].tools == (BALANCE_TOOL,)
    assert provider.calls[0].model == "m"


async def test_add_turn_appends_to_script() -> None:
    provider = FakeProvider()
    provider.add_turn(TextDelta(text="a"), Usage(input_tokens=1, output_tokens=1))
    assert await collect(provider, [USER]) == [
        TextDelta(text="a"),
        Usage(input_tokens=1, output_tokens=1),
    ]


async def test_exhausted_script_fails_loudly() -> None:
    provider = FakeProvider()
    with pytest.raises(FakeScriptExhaustedError):
        await collect(provider, [USER])
    assert len(provider.calls) == 1


async def test_raise_step_fails_mid_stream() -> None:
    provider = FakeProvider(
        [[TextDelta(text="Зачекайте"), Raise(ProviderError("503", retryable=True))]]
    )
    received: list[StreamEvent] = []
    with pytest.raises(ProviderError) as exc_info:
        await consume_into(provider, received)
    assert received == [TextDelta(text="Зачекайте")]
    assert exc_info.value.retryable


async def test_delay_hits_timeout_before_first_token() -> None:
    provider = FakeProvider([[Delay(1.0), TextDelta(text="пізно")]])
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await collect(provider, [USER])


async def test_text_turn_delay_spaces_out_deltas() -> None:
    steps = text_turn("абвгд", chunk_size=2, delay=0.5)
    assert steps == [
        Delay(0.5),
        TextDelta(text="аб"),
        Delay(0.5),
        TextDelta(text="вг"),
        Delay(0.5),
        TextDelta(text="д"),
    ]
    with pytest.raises(ValueError, match="chunk_size"):
        text_turn("x", chunk_size=0)


async def test_cancellation_stops_the_stream() -> None:
    provider = FakeProvider([[TextDelta(text="a"), Delay(10), TextDelta(text="b")]])
    received: list[StreamEvent] = []

    task = asyncio.create_task(consume_into(provider, received))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert received == [TextDelta(text="a")]


# --- registry --------------------------------------------------------------------


def test_registry_creates_provider_by_name() -> None:
    registry = ProviderRegistry()
    seen: list[ProviderConfig] = []

    def factory(config: ProviderConfig) -> LLMProvider:
        seen.append(config)
        return FakeProvider()

    registry.register("fake", factory)
    config = ProviderConfig(api_key=SecretStr("sk-test-123456"))

    assert isinstance(registry.create("fake", config), FakeProvider)
    assert seen == [config]
    assert registry.names == {"fake"}


def test_registry_rejects_unknown_and_duplicate_names() -> None:
    registry = ProviderRegistry()
    registry.register("fake", lambda _: FakeProvider())

    with pytest.raises(UnknownProviderError, match=r"'anthropic'.*known: fake"):
        registry.create("anthropic", ProviderConfig())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fake", lambda _: FakeProvider())


def test_provider_config_hides_api_key() -> None:
    config = ProviderConfig(api_key=SecretStr("sk-secret-value"))
    assert "sk-secret-value" not in repr(config)
