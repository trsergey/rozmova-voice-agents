"""The live-version cache record and signal shared by admin-api and the gateway."""

from uuid import uuid4

from fakeredis import FakeAsyncRedis

from rozmova_core import live_bots


async def test_announce_writes_then_signals() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    live = live_bots.LiveBot(
        bot_id=uuid4(),
        version_id=uuid4(),
        version=3,
        provider="openai",
        model="gpt-4.1-mini",
        flow={"nodes": [], "edges": []},
    )
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(live_bots.PUBLISHED_CHANNEL)
        await pubsub.get_message(timeout=1)

        await live_bots.announce(redis, live)

        signal = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert signal is not None
    assert signal["data"] == str(live.bot_id)
    assert await live_bots.read(redis, live.bot_id) == live
    assert await redis.ttl(live_bots.live_key(live.bot_id)) == -1  # no TTL
    assert await live_bots.read(redis, uuid4()) is None
