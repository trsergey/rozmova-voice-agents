"""Publish and rollback: immutable ``bot_versions`` and the live pointer (FR-7, FR-8, ADR 0012).

Both run in one PostgreSQL transaction that locks the bot row, so concurrent publishes get
consecutive version numbers. After the commit the live version goes to the Redis cache and
``bot:published`` (``rozmova_core.live_bots``). The cache is not part of the transaction:
if Redis fails there, the version is still live in PostgreSQL, which the gateway falls back
to, and the failure is logged.

Rollback never moves the pointer back: it creates version n+1 with the old flow and a note,
so the history shows every change of the live flow. It leaves the draft alone.

Publish also needs a green eval of the current draft (``admin_api.evals``, FR-19), whose
report the version keeps; a rollback copies the old version's report with its flow.

Everyone logged in reads the history, one version with its flow, and the diff between two
versions, or between a version and the draft (``…/diff/draft``: what the next publish would
change; FR-12).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly, CurrentSession
from admin_api.bots import KNOWN_TOOLS, draft_flow, get_bot
from admin_api.errors import ApiError
from admin_api.evals import require_green_eval
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.models import Bot, BotVersion, User
from rozmova_core.settings import Settings
from rozmova_engine.evals.report import EvalReport
from rozmova_engine.flow import FlowDiff, diff_flows, validate_flow

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


class VersionEvalOut(BaseModel):
    """The eval that let a version go live; ``None`` on versions from before the gate,
    from the seed or published with the gate bypassed."""

    passed: bool
    cases: int
    cases_passed: int
    finished_at: datetime


class VersionItemOut(VersionOut):
    created_by_email: str | None
    live: bool
    eval: VersionEvalOut | None = None


def _version_eval(version: BotVersion) -> VersionEvalOut | None:
    if version.eval_report is None:
        return None
    try:
        report = EvalReport.model_validate(version.eval_report)
    except ValidationError:
        log.warning("eval_report_unreadable", version_id=str(version.id))
        return None
    return VersionEvalOut(
        passed=report.summary.passed,
        cases=report.summary.cases,
        cases_passed=report.summary.cases_passed,
        finished_at=report.finished_at,
    )


class VersionFlowOut(VersionOut):
    flow: FlowGraph


class VersionDiffOut(BaseModel):
    before: int
    after: int | None  # None: the draft
    diff: FlowDiff


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


async def _live_flow(db: AsyncSession, bot: Bot) -> FlowGraph | None:
    if bot.live_version_id is None:
        return None
    live = await db.get(BotVersion, bot.live_version_id)
    return None if live is None else FlowGraph.model_validate(live.flow)


async def make_live(
    db: AsyncSession,
    bot: Bot,
    flow: FlowGraph,
    note: str | None,
    admin_id: str | None,
    eval_report: dict[str, Any] | None = None,
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
        eval_report=eval_report,
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


async def _version(db: AsyncSession, bot_id: uuid.UUID, version: int) -> BotVersion:
    found = await db.scalar(
        select(BotVersion).where(BotVersion.bot_id == bot_id, BotVersion.version == version)
    )
    if found is None:
        raise ApiError(404, "version_not_found", f"Bot has no version {version}.")
    return found


router = APIRouter(prefix="/bots", tags=["versions"])


@router.get("/{bot_id}/versions")
async def list_versions(
    bot_id: uuid.UUID, request: Request, _: CurrentSession
) -> list[VersionItemOut]:
    """The bot's versions, newest first."""
    async with _db(request)() as db:
        bot = await get_bot(db, bot_id)
        rows = await db.execute(
            select(BotVersion, User.email)
            .outerjoin(User, User.id == BotVersion.created_by)
            .where(BotVersion.bot_id == bot_id)
            .order_by(BotVersion.version.desc())
        )
        return [
            VersionItemOut(
                id=version.id,
                version=version.version,
                note=version.note,
                created_at=version.created_at,
                created_by=version.created_by,
                created_by_email=email,
                live=version.id == bot.live_version_id,
                eval=_version_eval(version),
            )
            for version, email in rows
        ]


@router.get("/{bot_id}/versions/{version}")
async def read_version(
    bot_id: uuid.UUID, version: int, request: Request, _: CurrentSession
) -> VersionFlowOut:
    """One version with its flow, e.g. for the editor to start over from what is live."""
    async with _db(request)() as db:
        await get_bot(db, bot_id)
        found = await _version(db, bot_id, version)
        return VersionFlowOut(
            id=found.id,
            version=found.version,
            note=found.note,
            created_at=found.created_at,
            created_by=found.created_by,
            flow=FlowGraph.model_validate(found.flow),
        )


@router.get("/{bot_id}/versions/{a}/diff/{b}")
async def diff_versions(
    bot_id: uuid.UUID,
    a: int,
    b: Annotated[str, Path(pattern=r"^([1-9][0-9]*|draft)$", description="A version or `draft`.")],
    request: Request,
    _: CurrentSession,
) -> VersionDiffOut:
    """What changes from version ``a`` to version ``b`` (or the draft): nodes and edges,
    without canvas positions."""
    async with _db(request)() as db:
        bot = await get_bot(db, bot_id)
        before = FlowGraph.model_validate((await _version(db, bot_id, a)).flow)
        after_version = None if b == "draft" else int(b)
        if after_version is None:
            after = draft_flow(bot)
        else:
            after = FlowGraph.model_validate((await _version(db, bot_id, after_version)).flow)
    return VersionDiffOut(before=a, after=after_version, diff=diff_flows(before, after))


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
        settings: Settings = request.app.state.settings
        report = None if settings.eval_gate_bypass else require_green_eval(bot, flow)
        version = await make_live(db, bot, flow, body.note or None, admin.user_id, report)
    await announce_live(request.app.state.redis, bot, version)
    log.info("published", bot_id=str(bot_id), version=version.version, user_id=admin.user_id)
    return VersionOut.model_validate(version)


@router.post("/{bot_id}/rollback/{version}", status_code=201)
async def rollback(
    bot_id: uuid.UUID, version: int, request: Request, admin: AdminOnly
) -> VersionOut:
    async with _db(request)() as db, db.begin():
        bot = await get_bot(db, bot_id, for_update=True)
        target = await _version(db, bot_id, version)
        flow = FlowGraph.model_validate(target.flow)
        created = await make_live(
            db, bot, flow, f"rollback of v{version}", admin.user_id, target.eval_report
        )
    await announce_live(request.app.state.redis, bot, created)
    log.info(
        "rolled_back",
        bot_id=str(bot_id),
        to=version,
        version=created.version,
        user_id=admin.user_id,
    )
    return VersionOut.model_validate(created)
