"""Resetting the demo by hand (ROZ-45): three admin-only buttons on the "Операції" page.

Each one clears one area and leaves the others alone:

- calls: every call and its events (``call_events`` go with them by cascade);
- the demo bot: name, provider, model and draft back to the seed's, and the seed flow
  published as a new version if another one is live. Versions stay (ADR 0012), the bot's
  provider key stays, other bots are not touched;
- sessions: the reconnect state in Redis (``session:{call_id}``); a call in progress keeps
  going, it only cannot be resumed after a drop.

The mock billing needs no button: the gateway makes a fresh one for every call.
"""

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly
from admin_api.errors import ApiError
from admin_api.publishing import announce_live, make_live
from admin_api.seed import DEMO_BOT_NAME, DEMO_MODEL, DEMO_PROVIDER, load_demo_flow
from rozmova_contracts.flow import FlowGraph
from rozmova_core.models import Bot, BotVersion, Call
from rozmova_core.settings import Settings

RESET_NOTE = "reset to the seed flow"
SESSION_PATTERN = "session:*"

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ops/reset", tags=["ops"])


class Deleted(BaseModel):
    deleted: int


class DemoBotReset(BaseModel):
    live_version: int
    # A new version was published: something other than the seed flow was live.
    republished: bool


@router.post("/calls")
async def reset_calls(request: Request, admin: AdminOnly) -> Deleted:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    async with factory() as db, db.begin():
        result = await db.execute(delete(Call))
    request.app.state.ops_cache.clear()
    deleted = int(getattr(result, "rowcount", 0) or 0)
    log.info("demo_reset", area="calls", deleted=deleted, user_id=admin.user_id)
    return Deleted(deleted=deleted)


@router.post("/sessions")
async def reset_sessions(request: Request, admin: AdminOnly) -> Deleted:
    redis: Redis = request.app.state.redis
    keys = [key async for key in redis.scan_iter(match=SESSION_PATTERN, count=500)]
    deleted = await redis.delete(*keys) if keys else 0
    log.info("demo_reset", area="sessions", deleted=deleted, user_id=admin.user_id)
    return Deleted(deleted=deleted)


@router.post("/demo-bot")
async def reset_demo_bot(request: Request, admin: AdminOnly) -> DemoBotReset:
    settings: Settings = request.app.state.settings
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    flow = load_demo_flow(settings)
    async with factory() as db, db.begin():
        bot = await db.get(Bot, settings.demo_bot_id, with_for_update=True)
        if bot is None:
            raise ApiError(404, "demo_bot_missing", "The demo bot does not exist here.")
        bot.name, bot.provider, bot.model = DEMO_BOT_NAME, DEMO_PROVIDER, DEMO_MODEL
        bot.draft_flow = flow.model_dump(mode="json")
        bot.draft_updated_at = func.now()
        bot.draft_eval_report = None
        live = await db.get(BotVersion, bot.live_version_id) if bot.live_version_id else None
        if live is not None and FlowGraph.model_validate(live.flow).same_logic(flow):
            republished = False
        else:
            # As the seed's own version, so later flow file changes keep reaching the bot.
            live = await make_live(db, bot, flow, RESET_NOTE, None)
            republished = True
    # Also when the flow was live already: the cache carries provider and model.
    await announce_live(request.app.state.redis, bot, live)
    log.info(
        "demo_reset",
        area="demo_bot",
        version=live.version,
        republished=republished,
        user_id=admin.user_id,
    )
    return DemoBotReset(live_version=live.version, republished=republished)
