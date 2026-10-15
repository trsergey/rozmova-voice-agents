"""Publish and rollback: immutable ``bot_versions`` and the live pointer (FR-7, FR-8, ADR 0012).

Both run in one PostgreSQL transaction that locks the bot row, so concurrent publishes get
consecutive version numbers. After the commit the live version goes to the Redis cache and
``bot:published`` (``rozmova_core.live_bots``). The cache is not part of the transaction:
if Redis fails there, the version is still live in PostgreSQL, which the gateway falls back
to, and the failure is logged.

Rollback never moves the pointer back: it creates version n+1 with the old flow and a note,
so the history shows every change of the live flow. It leaves the draft alone.
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StringConstraints
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly
from admin_api.bots import KNOWN_TOOLS, draft_flow, get_bot
from admin_api.errors import ApiError
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.models import Bot, BotVersion
from rozmova_engine.flow import validate_flow

log = structlog.get_logger(__name__)


class PublishRequest(BaseModel):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    note: str | None
    created_at: datetime
    created_by: uuid.UUID | None


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


async def _live_flow(db: AsyncSession, bot: Bot) -> FlowGraph | None:
    if bot.live_version_id is None:
        return None
    live = await db.get(BotVersion, bot.live_version_id)
    return None if live is None else FlowGraph.model_validate(live.flow)


async def make_live(
    db: AsyncSession, bot: Bot, flow: FlowGraph, note: str | None, admin_id: str | None
) -> BotVersion:
    """Append version n+1 with ``flow`` and point the bot at it. The bot row must be locked."""
    live = await _live_flow(db, bot)
    if live is not None and live.same_logic(flow):
        raise ApiError(409, "nothing_to_publish", "This flow is already live.")
    last = await db.scalar(
        select(func.coalesce(func.max(BotVersion.version), 0)).where(BotVersion.bot_id == bot.id)
    )
    version = BotVersion(
        bot_id=bot.id,
        version=(last or 0) + 1,
        flow=flow.model_dump(mode="json"),
        note=note,
        created_by=uuid.UUID(admin_id) if admin_id else None,
    )
    db.add(version)
    await db.flush()
    await db.refresh(version)
    bot.live_version_id = version.id
    return version


async def announce_live(redis: Redis, bot: Bot, version: BotVersion) -> None:
    """Put a committed live version into the cache and signal it; a Redis failure is logged."""
    live = live_bots.LiveBot(
        bot_id=bot.id,
        version_id=version.id,
        version=version.version,
        provider=bot.provider,
        model=bot.model,
        flow=version.flow,
    )
    try:
        await live_bots.announce(redis, live)
    except (RedisError, OSError):
        log.exception("live_cache_write_failed", bot_id=str(bot.id), version=version.version)


router = APIRouter(prefix="/bots", tags=["versions"])


@router.post("/{bot_id}/publish", status_code=201)
async def publish(
    bot_id: uuid.UUID, body: PublishRequest, request: Request, admin: AdminOnly
) -> VersionOut:
    async with _db(request)() as db, db.begin():
        bot = await get_bot(db, bot_id, for_update=True)
        flow = draft_flow(bot)
        result = validate_flow(flow, KNOWN_TOOLS)
        if not result.ok:
            raise ApiError(
                422,
                "draft_invalid",
                f"The draft has {len(result.errors)} error(s); fix them before publishing.",
                {"errors": [issue.model_dump(mode="json") for issue in result.errors]},
            )
        # ROZ-73 adds the eval gate here: a green report for flow.logic_hash().
        version = await make_live(db, bot, flow, body.note or None, admin.user_id)
    await announce_live(request.app.state.redis, bot, version)
    log.info("published", bot_id=str(bot_id), version=version.version, user_id=admin.user_id)
    return VersionOut.model_validate(version)


@router.post("/{bot_id}/rollback/{version}", status_code=201)
async def rollback(
    bot_id: uuid.UUID, version: int, request: Request, admin: AdminOnly
) -> VersionOut:
    async with _db(request)() as db, db.begin():
        bot = await get_bot(db, bot_id, for_update=True)
        target = await db.scalar(
            select(BotVersion).where(BotVersion.bot_id == bot_id, BotVersion.version == version)
        )
        if target is None:
            raise ApiError(404, "version_not_found", f"Bot has no version {version}.")
        flow = FlowGraph.model_validate(target.flow)
        created = await make_live(db, bot, flow, f"rollback of v{version}", admin.user_id)
    await announce_live(request.app.state.redis, bot, created)
    log.info(
        "rolled_back",
        bot_id=str(bot_id),
        to=version,
        version=created.version,
        user_id=admin.user_id,
    )
    return VersionOut.model_validate(created)
