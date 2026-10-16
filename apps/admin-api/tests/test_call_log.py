"""The call log consumer against a migrated PostgreSQL and fake (or real) Redis (ROZ-62)."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from admin_api import call_log
from admin_api.call_log import GROUP, LIVE_CHANNEL, CallLogConsumer
from rozmova_core.call_events import STREAM, CallEvent
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import AccessCode, Bot, BotVersion, Call, User
from rozmova_core.models import CallEvent as CallEventRow
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 16, 19, 0, tzinfo=UTC)


class World:
    """A bot with a published v1, a user and an access code, and the call log's stores."""

    def __init__(self, db: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self.db = db
        self.redis = redis
        self.bot_id = uuid4()
        self.version_id = uuid4()
        self.user_id = uuid4()
        self.code_id = uuid4()

    async def seed(self) -> None:
        async with self.db() as db, db.begin():
            db.add(Bot(id=self.bot_id, name="demo", provider="openai", model="gpt-4.1-mini"))
            await db.flush()
            db.add(BotVersion(id=self.version_id, bot_id=self.bot_id, version=1, flow={}))
            db.add(
                User(
                    id=self.user_id,
                    email=f"{self.user_id}@x",
                    password_hash="x",  # noqa: S106 - not a real hash
                    role="admin",
                )
            )
            db.add(AccessCode(id=self.code_id, label="acme", code_hash=str(self.code_id)))

    async def add(
        self,
        call_id: UUID,
        type_: str,
        /,
        *,
        at: int = 0,
        bot_id: UUID | None = None,
        **payload: Any,
    ) -> str:
        event = CallEvent(
            call_id=call_id,
            bot_id=bot_id or self.bot_id,
            type=type_,
            payload=payload,
            ts=T0 + timedelta(seconds=at),
        )
        entry_id: Any = await self.redis.xadd(STREAM, event.fields())  # type: ignore[arg-type]
        return str(entry_id)

    async def call(self, call_id: UUID) -> Call | None:
        async with self.db() as db:
            return await db.get(Call, call_id)

    async def events(self, call_id: UUID) -> list[tuple[str, dict[str, Any]]]:
        async with self.db() as db:
            rows = await db.scalars(
                select(CallEventRow)
                .where(CallEventRow.call_id == call_id)
                .order_by(CallEventRow.id)
            )
            return [(row.type, row.payload) for row in rows]

    async def pending(self) -> int:
        info: Any = await self.redis.xpending(STREAM, GROUP)
        return int(info["pending"])


@pytest.fixture
async def engine(database_url: SecretStr) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(env="test", database_url=database_url))
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Call))
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        await db.execute(delete(AccessCode))
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:
    redis = FakeAsyncRedis(decode_responses=True)
    world = World(create_session_factory(engine), redis)
    await world.seed()
    yield world
    await redis.aclose()


async def consume_all(consumer: CallLogConsumer) -> None:
    """One pass: create the group, read what is new, persist it."""
    await consumer.ensure_group()
    while entries := await consumer.read(pending=False):
        await consumer.persist(entries)


def consumer(world: World, name: str = "test-1", **kwargs: Any) -> CallLogConsumer:
    return CallLogConsumer(world.redis, world.db, name=name, poll_seconds=0.01, **kwargs)


async def test_a_call_becomes_a_calls_row_and_its_timeline(world: World) -> None:
    call_id = uuid4()
    live = world.redis.pubsub()
    await live.subscribe(LIVE_CHANNEL)
    await live.get_message(timeout=1)  # the subscribe confirmation

    await world.add(
        call_id,
        "session_started",
        bot_version=1,
        resumed=False,
        user_id=f"code:{world.code_id}",
        debug=False,
        call_id=str(call_id),
    )
    await world.add(call_id, "user_message", at=1, text="скільки в мене грошей")
    await world.add(call_id, "tool_call", at=2, turn_id=2, name="get_balance", args={})
    await world.add(
        call_id, "assistant_message", at=3, turn_id=2, text="120,50 грн.", interrupted=False
    )
    await world.add(call_id, "handoff", at=4, reason="requested_or_unresolved")
    await world.add(call_id, "session_end", at=5, reason="handoff")
    await world.add(call_id, "session_closed", at=5, code=1000, resumable=False)
    await consume_all(consumer(world))

    call = await world.call(call_id)
    assert call is not None
    assert call.bot_id == world.bot_id
    assert call.bot_version_id == world.version_id
    assert (call.user_id, call.access_code_id) == (None, world.code_id)
    assert call.started_at == T0
    assert call.ended_at == T0 + timedelta(seconds=5)
    assert call.outcome == "handoff"

    timeline = await world.events(call_id)
    assert [t for t, _ in timeline] == [
        "session_started",
        "user_message",
        "tool_call",
        "assistant_message",
        "handoff",
        "session_end",
        "session_closed",
    ]
    assert timeline[1][1] == {"text": "скільки в мене грошей"}
    assert await world.pending() == 0  # all acknowledged

    published = []
    while (message := await live.get_message(timeout=0.2)) is not None:
        published.append(json.loads(message["data"]))
    assert [m["type"] for m in published] == [t for t, _ in timeline]
    assert published[1]["call_id"] == str(call_id)
    assert published[1]["payload"] == {"text": "скільки в мене грошей"}
    await live.aclose()  # type: ignore[no-untyped-call]


async def test_a_redelivered_entry_is_stored_once(world: World) -> None:
    call_id = uuid4()
    await world.add(call_id, "session_started", bot_version=1, user_id="user:x", debug=True)
    await world.add(call_id, "user_message", at=1, text="привіт")
    c = consumer(world)
    await c.ensure_group()
    entries = await c.read(pending=False)

    await c.persist(entries)
    await c.persist(entries)  # at-least-once: the same entries again

    assert len(await world.events(call_id)) == 2


async def test_references_that_do_not_exist_are_stored_as_null(world: World) -> None:
    gone_code, viewer, admin = uuid4(), uuid4(), uuid4()
    await world.add(gone_code, "session_started", bot_version=99, user_id=f"code:{uuid4()}")
    await world.add(viewer, "session_started", bot_version=None, user_id="viewer-1")
    await world.add(admin, "session_started", bot_version=1, user_id=f"user:{world.user_id}")
    await consume_all(consumer(world))

    for call_id in (gone_code, viewer):
        call = await world.call(call_id)
        assert call is not None
        assert (call.bot_version_id, call.user_id, call.access_code_id) == (None, None, None)
    admin_call = await world.call(admin)
    assert admin_call is not None
    assert (admin_call.user_id, admin_call.access_code_id) == (world.user_id, None)


async def test_an_event_before_session_started_creates_the_row_that_start_completes(
    world: World,
) -> None:
    call_id = uuid4()
    await world.add(call_id, "node_enter", at=2, node_id="assist")
    await consume_all(consumer(world))
    early = await world.call(call_id)
    assert early is not None
    assert (early.started_at, early.bot_version_id) == (T0 + timedelta(seconds=2), None)

    await world.add(
        call_id, "session_started", at=1, bot_version=1, user_id=f"code:{world.code_id}"
    )
    await consume_all(consumer(world))

    call = await world.call(call_id)
    assert call is not None
    assert call.started_at == T0 + timedelta(seconds=1)
    assert (call.bot_version_id, call.access_code_id) == (world.version_id, world.code_id)


async def test_events_of_an_unknown_bot_and_malformed_entries_are_acknowledged(
    world: World,
) -> None:
    good, orphan = uuid4(), uuid4()
    await world.add(orphan, "session_started", bot_id=uuid4(), bot_version=1, user_id="x")
    await world.redis.xadd(STREAM, {"type": "broken"})
    await world.add(good, "session_started", bot_version=1, user_id="x")
    await consume_all(consumer(world))

    assert await world.call(orphan) is None
    assert [t for t, _ in await world.events(good)] == ["session_started"]
    assert await world.pending() == 0


async def test_entries_left_by_a_crashed_consumer_are_claimed_on_start(world: World) -> None:
    call_id = uuid4()
    await world.add(call_id, "session_started", bot_version=1, user_id="x")
    await world.add(call_id, "user_message", at=1, text="привіт")
    crashed = consumer(world, "crashed")
    await crashed.ensure_group()
    assert len(await crashed.read(pending=False)) == 2  # read, never persisted or acked
    assert await world.pending() == 2

    await consumer(world, "next", claim_idle_ms=0).claim_stale()

    assert [t for t, _ in await world.events(call_id)] == ["session_started", "user_message"]
    assert await world.pending() == 0


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


async def test_claim_and_redelivery_on_a_real_redis(real_world: World) -> None:
    """XGROUP, XREADGROUP, XAUTOCLAIM and XACK as a real server answers them."""
    world = real_world
    call_id = uuid4()
    await world.add(call_id, "session_started", bot_version=1, user_id=f"code:{world.code_id}")
    await world.add(call_id, "session_end", at=3, reason="completed")
    crashed = consumer(world, "crashed")
    await crashed.ensure_group()
    await crashed.ensure_group()  # BUSYGROUP is fine
    await crashed.read(pending=False)

    await consumer(world, "next", claim_idle_ms=0).claim_stale()

    call = await world.call(call_id)
    assert call is not None
    assert (call.outcome, call.access_code_id) == ("completed", world.code_id)
    assert await world.pending() == 0


@pytest.fixture
async def real_world(engine: AsyncEngine, redis_url: str) -> AsyncIterator[World]:
    redis = create_redis(Settings(redis_url=SecretStr(redis_url)))
    await redis.delete(STREAM)
    world = World(create_session_factory(engine), redis)
    await world.seed()
    yield world
    await redis.aclose()


async def until_stored(world: World, call_id: UUID, count: int) -> None:
    for _ in range(250):
        if len(await world.events(call_id)) >= count:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"fewer than {count} events stored in 5 s")


async def test_run_retries_a_failed_commit_and_stops_on_cancel(
    real_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop as admin-api runs it, on a real Redis."""
    world = real_world
    monkeypatch.setattr(call_log, "RETRY_SECONDS", (0.01,))
    failures = [RuntimeError("database is down")]
    real_store = call_log._store

    async def flaky_store(db: AsyncSession, entry_id: str, event: CallEvent) -> bool:
        if failures:
            raise failures.pop()
        return await real_store(db, entry_id, event)

    monkeypatch.setattr(call_log, "_store", flaky_store)
    call_id = uuid4()
    await world.add(call_id, "session_started", bot_version=1, user_id="x")
    task = asyncio.create_task(consumer(world).run())
    try:
        await until_stored(world, call_id, 1)
        await world.add(call_id, "user_message", at=1, text="ще")  # read from ">" now
        await until_stored(world, call_id, 2)
    finally:
        task.cancel()
        await asyncio.wait([task])
    assert task.cancelled()
    assert await world.pending() == 0
