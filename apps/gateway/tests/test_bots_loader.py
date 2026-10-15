"""``PublishedBotLoader`` against a migrated PostgreSQL and a fake Redis."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from sqlalchemy import delete

from gateway.bots import BotUnavailableError, Providers, PublishedBotLoader
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, BotVersion
from rozmova_core.settings import Settings
from rozmova_engine.providers import ProviderConfig
from rozmova_engine.providers.fake import FakeProvider

DEMO_FLOW: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)


def flow(greeting: str) -> dict[str, Any]:
    raw = FlowGraph.model_validate(DEMO_FLOW).model_dump(mode="json")
    [start] = [n for n in raw["nodes"] if n["type"] == "start"]
    start["data"]["greeting"] = greeting
    return raw


def greeting(runtime: Any) -> str:
    [start] = [n for n in runtime.flow.nodes if n.type == "start"]
    text: str = start.data.greeting
    return text


class FakeProviders(Providers):
    """No real SDK clients: every provider name gets one fake."""

    def __init__(self) -> None:
        super().__init__({})
        self.fake = FakeProvider([])

    def get(self, name: str) -> Any:
        return self.fake


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Db:
    """Bots and versions written the way admin-api writes them."""

    def __init__(self, settings: Settings) -> None:
        self.engine = create_engine(settings)
        self.factory = create_session_factory(self.engine)

    async def bot(self, draft: dict[str, Any] | None = None) -> UUID:
        async with self.factory() as db, db.begin():
            bot = Bot(name="b", provider="openai", model="gpt-4.1-mini", draft_flow=draft)
            db.add(bot)
            await db.flush()
            return bot.id

    async def publish(self, bot_id: UUID, version: int, flow: dict[str, Any]) -> BotVersion:
        async with self.factory() as db, db.begin():
            row = BotVersion(bot_id=bot_id, version=version, flow=flow)
            db.add(row)
            await db.flush()
            bot = await db.get(Bot, bot_id)
            assert bot is not None
            bot.live_version_id = row.id
            return row


@pytest.fixture
async def db(database_url: SecretStr) -> AsyncIterator[Db]:
    db = Db(Settings(database_url=database_url))
    async with db.factory() as session, session.begin():
        await session.execute(delete(Bot))
    yield db
    await db.engine.dispose()


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
async def loader(
    database_url: SecretStr, redis: FakeAsyncRedis
) -> AsyncIterator[tuple[PublishedBotLoader, Clock]]:
    clock = Clock()
    loader = PublishedBotLoader(
        redis, create_engine(Settings(database_url=database_url)), FakeProviders(), clock=clock
    )
    yield loader, clock
    await loader.aclose()


Loader = tuple[PublishedBotLoader, Clock]


def live(bot_id: UUID, version: int, raw: dict[str, Any]) -> live_bots.LiveBot:
    return live_bots.LiveBot(
        bot_id=bot_id,
        version_id=uuid4(),
        version=version,
        provider="openai",
        model="gpt-4.1-mini",
        flow=raw,
    )


async def announce(redis: FakeAsyncRedis, bot_id: UUID, version: int, raw: dict[str, Any]) -> None:
    await live_bots.announce(redis, live(bot_id, version, raw))


async def version_of(loader: PublishedBotLoader, bot_id: UUID) -> int | None:
    runtime = await loader.load(bot_id)
    assert runtime is not None
    return runtime.bot_version


@pytest.mark.integration
async def test_live_version_comes_from_redis(db: Db, redis: FakeAsyncRedis, loader: Loader) -> None:
    bot_id = await db.bot()
    await db.publish(bot_id, 1, flow("з бази"))
    await announce(redis, bot_id, 1, flow("з кешу"))

    runtime = await loader[0].load(bot_id)

    assert runtime is not None
    assert (runtime.bot_version, runtime.model, greeting(runtime)) == (1, "gpt-4.1-mini", "з кешу")


@pytest.mark.integration
async def test_empty_redis_falls_back_to_postgres(db: Db, loader: Loader) -> None:
    bot_id = await db.bot()
    await db.publish(bot_id, 1, flow("v1"))
    await db.publish(bot_id, 2, flow("v2"))

    runtime = await loader[0].load(bot_id)

    assert runtime is not None
    assert (runtime.bot_version, greeting(runtime)) == (2, "v2")


@pytest.mark.integration
async def test_unknown_or_unpublished_bot_is_none(db: Db, loader: Loader) -> None:
    unpublished = await db.bot(draft=flow("чернетка"))

    assert await loader[0].load(uuid4()) is None
    assert await loader[0].load(unpublished) is None


@pytest.mark.integration
async def test_local_cache_until_signal_or_ttl(
    db: Db, redis: FakeAsyncRedis, loader: Loader
) -> None:
    published, clock = loader
    bot_id = await db.bot()
    await announce(redis, bot_id, 1, flow("v1"))
    assert greeting(await published.load(bot_id)) == "v1"

    # Changed in Redis but the signal was lost: new calls still get the cached version...
    await redis.set(live_bots.live_key(bot_id), live(bot_id, 2, flow("v2")).model_dump_json())
    assert await version_of(published, bot_id) == 1
    # ...until the entry expires: a lost signal costs at most the TTL...
    clock.now += 31
    assert await version_of(published, bot_id) == 2

    # ...and the signal drops the entry at once.
    await redis.set(live_bots.live_key(bot_id), live(bot_id, 3, flow("v3")).model_dump_json())
    assert await version_of(published, bot_id) == 2
    published.invalidate(bot_id)
    assert greeting(await published.load(bot_id)) == "v3"


@pytest.mark.integration
async def test_watch_invalidates_on_bot_published(
    db: Db, redis: FakeAsyncRedis, loader: Loader
) -> None:
    published, _ = loader
    bot_id = await db.bot()
    await announce(redis, bot_id, 1, flow("v1"))
    watch = asyncio.create_task(published.watch())
    try:
        await asyncio.sleep(0.1)  # subscribed
        assert greeting(await published.load(bot_id)) == "v1"

        await announce(redis, bot_id, 2, flow("v2"))  # what admin-api's publish does

        async with asyncio.timeout(3):
            # Polling: the signal arrives on the watch task, with nothing here to wait on.
            while greeting(await published.load(bot_id)) != "v2":  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        watch.cancel()
        await asyncio.wait([watch])


@pytest.mark.integration
async def test_pinned_version_and_draft(db: Db, redis: FakeAsyncRedis, loader: Loader) -> None:
    bot_id = await db.bot(draft=flow("чернетка"))
    await db.publish(bot_id, 1, flow("v1"))
    await db.publish(bot_id, 2, flow("v2"))
    await announce(redis, bot_id, 2, flow("v2"))

    v1 = await loader[0].load(bot_id, version=1)
    draft = await loader[0].load(bot_id, draft=True)

    assert v1 is not None
    assert (v1.bot_version, greeting(v1)) == (1, "v1")
    assert draft is not None
    assert (draft.bot_version, greeting(draft)) == (None, "чернетка")
    assert await loader[0].load(bot_id, version=9) is None


@pytest.mark.integration
async def test_database_down_is_unavailable(redis: FakeAsyncRedis) -> None:
    down = Settings(database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/none"))
    loader = PublishedBotLoader(redis, create_engine(down), FakeProviders())
    try:
        with pytest.raises(BotUnavailableError):
            await loader.load(uuid4())
    finally:
        await loader.aclose()


def test_providers_reuse_one_client_per_name() -> None:
    providers = Providers({"openai": ProviderConfig(api_key=SecretStr("sk-test"))})

    assert providers.get("openai") is providers.get("openai")
