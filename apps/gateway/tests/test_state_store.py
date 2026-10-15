from uuid import uuid4

import pytest
from fakeredis import FakeAsyncRedis

from gateway.state_store import SESSION_TTL_SECONDS, SessionStore, session_key
from rozmova_engine.providers import Message
from rozmova_engine.session import HistoryEntry, SessionState, SessionStateError


def make_state() -> SessionState:
    return SessionState(
        call_id=uuid4(),
        bot_id=uuid4(),
        bot_version=3,
        turn=2,
        current_node="menu",
        history=[HistoryEntry(turn=1, message=Message(role="assistant", content="Вітаю!"))],
    )


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


async def test_no_state_for_a_new_call(redis: FakeAsyncRedis) -> None:
    store = SessionStore(redis, uuid4())

    assert await store.claim() is None
    assert await redis.keys() == []  # claiming a new call writes nothing


async def test_saved_state_is_resumed_by_the_next_connection(redis: FakeAsyncRedis) -> None:
    state = make_state()
    assert await SessionStore(redis, state.call_id).save(state)

    assert await SessionStore(redis, state.call_id).claim() == state


async def test_save_refreshes_the_ttl(redis: FakeAsyncRedis) -> None:
    state = make_state()
    store = SessionStore(redis, state.call_id)
    await store.save(state)
    await redis.expire(session_key(state.call_id), 5)

    await store.save(state)

    assert await redis.ttl(session_key(state.call_id)) == SESSION_TTL_SECONDS


async def test_superseded_connection_cannot_save_or_drop(redis: FakeAsyncRedis) -> None:
    state = make_state()
    old = SessionStore(redis, state.call_id)
    await old.save(state)

    new = SessionStore(redis, state.call_id)
    resumed = await new.claim()
    assert resumed is not None
    resumed.turn = 3
    assert await new.save(resumed)

    stale = state.model_copy(update={"turn": 2})
    assert await old.save(stale) is False
    await old.drop()

    current = await SessionStore(redis, state.call_id).claim()
    assert current is not None
    assert current.turn == 3


async def test_owner_drops_a_finished_call(redis: FakeAsyncRedis) -> None:
    state = make_state()
    store = SessionStore(redis, state.call_id)
    await store.save(state)

    await store.drop()

    assert await redis.exists(session_key(state.call_id)) == 0


async def test_unreadable_state_raises(redis: FakeAsyncRedis) -> None:
    call_id = uuid4()
    await redis.hset(session_key(call_id), mapping={"v": "0", "turn": "1"})

    with pytest.raises(SessionStateError):
        await SessionStore(redis, call_id).claim()
