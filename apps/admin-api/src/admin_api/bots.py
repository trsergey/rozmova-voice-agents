"""Bots and their in-place draft (FR-7, FR-9, ADR 0012).

A draft saves whenever its shape matches the ``FlowGraph`` contract, so a half-finished flow
is never lost; semantic problems come from ``POST /bots/{id}/validate``, which runs the
engine's validator on the saved draft. Concurrent edits: last write wins (ADR 0012).
Everyone logged in reads and validates; only admins create bots and save drafts.
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly, current_session
from admin_api.errors import ApiError
from rozmova_contracts.flow import FlowGraph
from rozmova_core.models import Bot, BotVersion
from rozmova_engine.flow import ValidationResult, validate_flow
from rozmova_engine.providers import registry as providers
from rozmova_engine.rag.answer import SEARCH_TOOL
from rozmova_engine.tools import MockBilling, ToolRegistry, subscriber_tools

log = structlog.get_logger(__name__)

# The tools a flow may call: the gateway registers the same set (bound to real billing
# and the knowledge base).
KNOWN_TOOLS = ToolRegistry(subscriber_tools(MockBilling([]))).names | {SEARCH_TOOL}

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class BotIn(BaseModel):
    name: Name
    provider: str
    model: Name

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, provider: str) -> str:
        if provider not in providers.names:
            raise ValueError(f"unknown provider; one of {sorted(providers.names)}")
        return provider


class BotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: str
    model: str
    live_version_id: uuid.UUID | None
    live_version: int | None = None  # the live version's number, e.g. 3 for "v3"
    draft_updated_at: datetime | None
    created_at: datetime


class DraftOut(BaseModel):
    flow: FlowGraph
    updated_at: datetime | None
    # SHA-256 of the flow without canvas positions; eval reports and publish compare it.
    hash: str


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


async def get_bot(db: AsyncSession, bot_id: uuid.UUID, *, for_update: bool = False) -> Bot:
    bot = await db.get(Bot, bot_id, with_for_update=for_update)
    if bot is None:
        raise ApiError(404, "bot_not_found", "No such bot.")
    return bot


def draft_flow(bot: Bot) -> FlowGraph:
    return FlowGraph.model_validate(bot.draft_flow or {})


def _draft_out(bot: Bot) -> DraftOut:
    flow = draft_flow(bot)
    return DraftOut(flow=flow, updated_at=bot.draft_updated_at, hash=flow.logic_hash())


async def _bot_out(db: AsyncSession, bot: Bot) -> BotOut:
    live = await db.get(BotVersion, bot.live_version_id) if bot.live_version_id else None
    return BotOut.model_validate(bot).model_copy(
        update={"live_version": live.version if live else None}
    )


router = APIRouter(prefix="/bots", tags=["bots"], dependencies=[Depends(current_session)])


@router.get("")
async def list_bots(request: Request) -> list[BotOut]:
    async with _db(request)() as db:
        rows = await db.execute(
            select(Bot, BotVersion.version)
            .outerjoin(BotVersion, Bot.live_version_id == BotVersion.id)
            .order_by(Bot.created_at)
        )
        return [
            BotOut.model_validate(bot).model_copy(update={"live_version": live})
            for bot, live in rows
        ]


@router.post("", status_code=201)
async def create_bot(body: BotIn, request: Request, admin: AdminOnly) -> BotOut:
    async with _db(request)() as db, db.begin():
        bot = Bot(
            name=body.name,
            provider=body.provider,
            model=body.model,
            draft_flow=FlowGraph().model_dump(mode="json"),
        )
        db.add(bot)
        await db.flush()
        await db.refresh(bot)
    log.info("bot_created", bot_id=str(bot.id), user_id=admin.user_id)
    return BotOut.model_validate(bot)


@router.get("/{bot_id}")
async def read_bot(bot_id: uuid.UUID, request: Request) -> BotOut:
    async with _db(request)() as db:
        return await _bot_out(db, await get_bot(db, bot_id))


@router.get("/{bot_id}/draft")
async def get_draft(bot_id: uuid.UUID, request: Request) -> DraftOut:
    async with _db(request)() as db:
        return _draft_out(await get_bot(db, bot_id))


@router.put("/{bot_id}/draft")
async def save_draft(
    bot_id: uuid.UUID, flow: FlowGraph, request: Request, admin: AdminOnly
) -> DraftOut:
    async with _db(request)() as db, db.begin():
        bot = await get_bot(db, bot_id)
        bot.draft_flow = flow.model_dump(mode="json")
        bot.draft_updated_at = await db.scalar(select(func.now()))
    out = _draft_out(bot)
    log.info("draft_saved", bot_id=str(bot_id), user_id=admin.user_id, hash=out.hash[:12])
    return out


@router.post("/{bot_id}/validate")
async def validate_draft(bot_id: uuid.UUID, request: Request) -> ValidationResult:
    """Read-only, so viewers may run it too; it still needs the CSRF header (a POST)."""
    async with _db(request)() as db:
        bot = await get_bot(db, bot_id)
    return validate_flow(draft_flow(bot), KNOWN_TOOLS)
