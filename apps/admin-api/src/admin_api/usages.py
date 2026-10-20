"""Where a tool is used (FR-18): which bots, versions and drafts call it.

A flow names a tool in two places: the ``tools`` an ``llm_prompt`` may call and the ``tool``
of a ``tool`` node. Both are JSONB containment (``@>``) queries, which the ``jsonb_path_ops``
GIN indexes on ``bot_versions.flow`` and ``bots.draft_flow`` serve. The name is not checked
against the tool catalog: the search matters most for a tool that is about to go away.
"""

import uuid
from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import current_session
from rozmova_core.models import Bot, BotVersion


class ToolUsageOut(BaseModel):
    bot_id: uuid.UUID
    bot_name: str
    versions: list[int]  # newest first
    live: bool  # the live version uses the tool
    draft: bool  # the draft uses the tool


def _uses(column: Any, tool: str) -> ColumnElement[bool]:
    return or_(
        column.contains({"nodes": [{"type": "llm_prompt", "data": {"tools": [tool]}}]}),
        column.contains({"nodes": [{"type": "tool", "data": {"tool": tool}}]}),
    )


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(current_session)])


@router.get("/usages")
async def tool_usages(
    tool: Annotated[str, Query(min_length=1, max_length=100)], request: Request
) -> list[ToolUsageOut]:
    """Bots whose versions or draft use ``tool``, by bot name."""
    async with _db(request)() as db:
        versions = await db.execute(
            select(BotVersion.bot_id, BotVersion.version, BotVersion.id)
            .where(_uses(BotVersion.flow, tool))
            .order_by(BotVersion.version.desc())
        )
        by_bot: dict[uuid.UUID, list[tuple[int, uuid.UUID]]] = defaultdict(list)
        for bot_id, version, version_id in versions:
            by_bot[bot_id].append((version, version_id))
        in_drafts = set(await db.scalars(select(Bot.id).where(_uses(Bot.draft_flow, tool))))
        bots = await db.scalars(
            select(Bot).where(Bot.id.in_(by_bot.keys() | in_drafts)).order_by(Bot.name, Bot.id)
        )
        return [
            ToolUsageOut(
                bot_id=bot.id,
                bot_name=bot.name,
                versions=[version for version, _ in by_bot[bot.id]],
                live=any(version_id == bot.live_version_id for _, version_id in by_bot[bot.id]),
                draft=bot.id in in_drafts,
            )
            for bot in bots
        ]
