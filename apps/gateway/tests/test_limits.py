import time
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway import limits
from gateway.app import client_ip
from gateway.limits import RateLimiter
from rozmova_core.settings import Settings

SETTINGS = Settings(limit_messages_per_minute=3, limit_sessions_per_user=2)


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(FakeAsyncRedis(decode_responses=True), SETTINGS)


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = time.time()
        monkeypatch.setattr("gateway.limits.time.time", lambda: self.now)


async def test_messages_over_the_rate_are_refused(limiter: RateLimiter) -> None:
    assert [await limiter.allow_message("u1") for _ in range(4)] == [True, True, True, False]
    assert await limiter.allow_message("u2")  # per user


async def test_message_rate_resets_next_minute(
    limiter: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(monkeypatch)
    for _ in range(3):
        await limiter.allow_message("u1")
    assert not await limiter.allow_message("u1")

    clock.now += 60

    assert await limiter.allow_message("u1")


async def test_redis_failure_lets_messages_through() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    limiter = RateLimiter(redis, SETTINGS)
    for _ in range(3):
        await limiter.allow_message("u1")
    assert not await limiter.allow_message("u1")

    async def down(*_: object, **__: object) -> None:
        raise RedisConnectionError("down")

    redis.connection_pool.get_connection = down  # type: ignore[method-assign]

    assert await limiter.allow_message("u1")


async def test_open_calls_are_limited_per_user(limiter: RateLimiter) -> None:
    assert await limiter.open_call("u1", "a")
    assert await limiter.open_call("u1", "b")
    assert not await limiter.open_call("u1", "c")
    assert await limiter.open_call("u2", "c")

    await limiter.close_call("u1", "a")

    assert await limiter.open_call("u1", "c")


async def test_calls_left_by_a_crashed_process_go_stale(
    limiter: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(monkeypatch)
    assert await limiter.open_call("u1", "a")
    assert await limiter.open_call("u1", "b")

    clock.now += SETTINGS.limit_session_seconds + limits.STALE_MARGIN_SECONDS + 1

    assert await limiter.open_call("u1", "c")


def test_client_ip_is_the_leftmost_forwarded_for() -> None:
    def ws(headers: dict[str, str], host: str = "10.0.0.1") -> object:
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))

    assert client_ip(ws({"x-forwarded-for": "203.0.113.7, 10.1.1.1"})) == "203.0.113.7"  # type: ignore[arg-type]
    assert client_ip(ws({})) == "10.0.0.1"  # type: ignore[arg-type]
