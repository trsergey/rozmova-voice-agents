"""Per-user demo limits (SDD "Безпека та контроль витрат"): the public demo must not burn
the LLM budget. Keys are ``ratelimit:{user_id}:{window}`` with the user_id from the ticket,
never the IP: behind Railway's proxy many viewers can share one, and it is easy to change.

- Messages: a fixed one-minute window, ``INCR`` + ``EXPIRE``. Simple and cheap; the price
  is that a burst across a minute boundary can reach twice the limit, fine for a demo.
- Open calls: a sorted set of connection ids scored by start time. Checking and adding is
  one Lua script, so two connections opened at once cannot both take the last place. A
  call lives at most ``limit_session_seconds``, so entries older than that are stale (a
  crashed process never removed them) and are pruned: the count cannot get stuck.

Turns and duration per call are enforced by the session itself (``CallLimits``).
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from rozmova_core.settings import Settings

log = structlog.get_logger(__name__)

# A stale entry outlives the longest possible call by this much.
STALE_MARGIN_SECONDS = 60

# KEYS[1] = set of open calls; ARGV = now, stale before, limit, member, TTL. 1 if admitted.
_OPEN_CALL = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""


@dataclass(frozen=True, slots=True)
class CallLimits:
    """What one session enforces itself; ``allow_message`` is the per-user message rate."""

    max_turns: int
    max_seconds: float
    allow_message: Callable[[], Awaitable[bool]]


class RateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._messages_per_minute = settings.limit_messages_per_minute
        self._calls_per_user = settings.limit_sessions_per_user
        self._call_seconds = settings.limit_session_seconds
        self._max_turns = settings.limit_turns_per_session
        self._open_call = redis.register_script(_OPEN_CALL)

    def for_call(self, user_id: str) -> CallLimits:
        async def allow_message() -> bool:
            return await self.allow_message(user_id)

        return CallLimits(
            max_turns=self._max_turns,
            max_seconds=self._call_seconds,
            allow_message=allow_message,
        )

    async def allow_message(self, user_id: str) -> bool:
        """Count a message; ``False`` once the user is over the per-minute limit.

        A Redis failure lets the message through: the demo keeps working, and the LLM
        spend limits on the keys still cap the cost.
        """
        key = f"ratelimit:{user_id}:msg:{int(time.time()) // 60}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 60)
                count, _ = await pipe.execute()
        except RedisError as err:
            log.warning("rate_limit_unavailable", error=str(err))
            return True
        return int(count) <= self._messages_per_minute

    async def open_call(self, user_id: str, connection_id: str) -> bool:
        """Take a place among the user's open calls; ``False`` if all are taken."""
        now = time.time()
        stale = self._call_seconds + STALE_MARGIN_SECONDS
        admitted = await self._open_call(
            keys=[self._calls_key(user_id)],
            args=[now, now - stale, self._calls_per_user, connection_id, stale],
        )
        return bool(admitted)

    async def close_call(self, user_id: str, connection_id: str) -> None:
        try:
            await self._redis.zrem(self._calls_key(user_id), connection_id)
        except RedisError as err:
            # The entry goes stale and is pruned after the longest possible call.
            log.warning("rate_limit_unavailable", error=str(err))

    @staticmethod
    def _calls_key(user_id: str) -> str:
        return f"ratelimit:{user_id}:calls"
