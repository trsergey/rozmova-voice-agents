"""The published-version cache shared by admin-api (writes) and the gateway (reads), ADR 0012.

``bot:{id}:live`` holds the live version as JSON, without a TTL; ``bot:published`` carries
the bot id whenever it changes, so gateways drop their in-process copy. PostgreSQL stays the
source of truth: a missing or stale key means "read ``bots.live_version_id``" (ROZ-51).
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

PUBLISHED_CHANNEL = "bot:published"


class LiveBot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_id: UUID
    version_id: UUID
    version: int
    provider: str
    model: str
    flow: dict[str, Any]  # a FlowGraph dump; the gateway validates it against the contract


def live_key(bot_id: UUID) -> str:
    return f"bot:{bot_id}:live"


async def announce(redis: Redis, live: LiveBot) -> None:
    """Write the cache, then signal: a gateway that hears the signal reads the new value."""
    await redis.set(live_key(live.bot_id), live.model_dump_json())
    await redis.publish(PUBLISHED_CHANNEL, str(live.bot_id))


async def read(redis: Redis, bot_id: UUID) -> LiveBot | None:
    raw = await redis.get(live_key(bot_id))
    return None if raw is None else LiveBot.model_validate_json(raw)


async def signal_changed(redis: Redis, bot_id: UUID) -> None:
    """Make gateways drop what they cached for the bot (its live version and its provider
    keys) without changing the live version itself, e.g. after a provider key changes."""
    await redis.publish(PUBLISHED_CHANNEL, str(bot_id))
