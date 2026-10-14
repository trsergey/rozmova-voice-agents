import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rozmova_contracts.protocol import HistoryMessage
from rozmova_engine.providers import Message, ToolCall
from rozmova_engine.session import (
    INTERRUPTED_MARK,
    HistoryEntry,
    PendingAction,
    SessionState,
    SessionStateError,
)

ACTIVATE = ToolCall(id="c1", name="activate_service", arguments={"service_id": "roaming_eu"})


def walkthrough_state() -> SessionState:
    """State after the S3 step 1 of the SDD walkthrough, plus an interrupted reply."""
    return SessionState(
        call_id=uuid4(),
        bot_id=uuid4(),
        bot_version=3,
        turn=2,
        current_node="assist",
        slots={"lang": "uk", "attempts": 1},
        pending_action=PendingAction(
            name="activate_service", args={"service_id": "roaming_eu"}, turn=2
        ),
        history=[
            HistoryEntry(
                turn=0, message=Message(role="assistant", content="Вітаю! Чим допомогти?")
            ),
            HistoryEntry(turn=1, message=Message(role="user", content="розкажи про тарифи")),
            HistoryEntry(
                turn=1,
                message=Message(role="assistant", content="У нас є тарифи Старт, Опти"),
                interrupted=True,
            ),
            HistoryEntry(turn=2, message=Message(role="user", content="підключіть роумінг Європа")),
            HistoryEntry(turn=2, message=Message(role="assistant", tool_calls=(ACTIVATE,))),
            HistoryEntry(
                turn=2,
                message=Message(
                    role="tool",
                    content='{"status": "confirmation_required", "price": "99 грн/міс"}',
                    tool_call_id="c1",
                ),
            ),
            HistoryEntry(
                turn=2,
                message=Message(
                    role="assistant", content="Роумінг Європа — 99 грн/міс. Підключити?"
                ),
            ),
        ],
    )


# --- model -----------------------------------------------------------------------


def test_new_session_defaults() -> None:
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=None)
    assert (state.turn, state.current_node, state.slots, state.history) == (0, None, {}, [])
    assert state.pending_action is None


def test_only_assistant_reply_can_be_interrupted() -> None:
    with pytest.raises(ValidationError):
        HistoryEntry(turn=1, message=Message(role="user", content="алло"), interrupted=True)


def test_llm_messages_mark_interrupted_reply() -> None:
    messages = walkthrough_state().llm_messages()

    assert messages[2].content == f"У нас є тарифи Старт, Опти {INTERRUPTED_MARK}"
    assert messages[4].tool_calls == (ACTIVATE,)
    assert len(messages) == 7


def test_interrupted_reply_without_text_is_only_the_mark() -> None:
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=None)
    state.history.append(HistoryEntry(turn=1, message=Message(role="assistant"), interrupted=True))
    assert state.llm_messages()[0].content == INTERRUPTED_MARK


def test_transcript_has_only_what_the_subscriber_saw() -> None:
    assert walkthrough_state().transcript() == [
        HistoryMessage(role="assistant", text="Вітаю! Чим допомогти?"),
        HistoryMessage(role="user", text="розкажи про тарифи"),
        HistoryMessage(role="assistant", text="У нас є тарифи Старт, Опти", interrupted=True),
        HistoryMessage(role="user", text="підключіть роумінг Європа"),
        HistoryMessage(role="assistant", text="Роумінг Європа — 99 грн/міс. Підключити?"),
    ]


# --- Redis hash form -------------------------------------------------------------


def test_round_trip_through_hash_fields() -> None:
    state = walkthrough_state()
    fields = state.to_redis()

    assert fields["v"] == "1"
    assert all(isinstance(value, str) for value in fields.values())
    assert SessionState.from_redis(fields) == state


def test_round_trip_from_raw_bytes() -> None:
    state = walkthrough_state()
    raw = {key.encode(): value.encode() for key, value in state.to_redis().items()}
    assert SessionState.from_redis(raw) == state


def test_hash_keeps_ukrainian_text_readable() -> None:
    history = json.loads(walkthrough_state().to_redis()["history"])
    assert history[1]["message"]["content"] == "розкажи про тарифи"


@pytest.mark.parametrize("version", [None, "0", "2"])
def test_other_format_version_is_rejected(version: str | None) -> None:
    fields = walkthrough_state().to_redis()
    del fields["v"]
    if version is not None:
        fields["v"] = version
    with pytest.raises(SessionStateError, match="unsupported session format"):
        SessionState.from_redis(fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [("history", "{not json"), ("turn", '"many"'), ("slots", "[1, 2]")],
)
def test_corrupt_field_is_rejected(field: str, value: str) -> None:
    fields = walkthrough_state().to_redis() | {field: value}
    with pytest.raises(SessionStateError, match="corrupt session state"):
        SessionState.from_redis(fields)


def test_missing_field_is_rejected() -> None:
    fields = walkthrough_state().to_redis()
    del fields["call_id"]
    with pytest.raises(SessionStateError):
        SessionState.from_redis(fields)


def test_empty_hash_is_rejected() -> None:
    # HGETALL on an expired key returns {}.
    with pytest.raises(SessionStateError):
        SessionState.from_redis({})


# --- real Redis ------------------------------------------------------------------


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


@pytest.mark.integration
@pytest.mark.parametrize("decode_responses", [True, False])
async def test_round_trip_through_real_redis(redis_url: str, decode_responses: bool) -> None:
    from redis.asyncio import Redis

    state = walkthrough_state()
    key = f"session:{state.call_id}"
    client = Redis.from_url(redis_url, decode_responses=decode_responses)
    try:
        await client.hset(key, mapping=state.to_redis())  # type: ignore[arg-type]
        assert SessionState.from_redis(await client.hgetall(key)) == state
    finally:
        await client.aclose()
