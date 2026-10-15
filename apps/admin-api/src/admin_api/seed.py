"""Data admin-api creates on every start, so a new environment works without manual SQL.

- The admin user from ``ROZMOVA_ADMIN_EMAIL`` / ``ROZMOVA_ADMIN_PASSWORD``; changing the
  variable is how the admin password is reset.
- The demo bot (ROZ-54): the SDD's Hvylia Mobile flow from ``flows/hvylia-demo.json``,
  saved as the draft and published as v1, under the fixed ``ROZMOVA_DEMO_BOT_ID`` the
  smoke test and the public widget call.
- When the file changes (ROZ-82), the next start publishes it as a new version, so a
  merged flow change is live after the deploy. Only while the bot is the seed's: its live
  version was published by the seed (``created_by`` empty) and the draft has no unpublished
  edits. Once a person publishes, rolls back or edits the draft in the admin, the seed
  leaves the bot alone and logs ``demo_bot_seed_skipped``.
"""

import asyncio

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import normalize_email
from admin_api.bots import KNOWN_TOOLS
from admin_api.passwords import hash_password, verify_password
from admin_api.publishing import announce_live, make_live
from rozmova_contracts.flow import FlowGraph
from rozmova_core.models import Bot, BotVersion, User
from rozmova_core.settings import Settings
from rozmova_engine.flow import validate_flow

DEMO_BOT_NAME = "Hvylia Mobile demo"
DEMO_PROVIDER = "openai"
DEMO_MODEL = "gpt-4.1-mini"
SEEDED_NOTE = "seeded demo flow"
REPUBLISHED_NOTE = "seed: flows/hvylia-demo.json changed"

log = structlog.get_logger(__name__)


async def seed_admin(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> None:
    if settings.admin_email is None or settings.admin_password is None:
        log.warning("admin_seed_skipped", reason="ROZMOVA_ADMIN_EMAIL or _PASSWORD unset")
        return
    email = normalize_email(settings.admin_email)
    password = settings.admin_password.get_secret_value()

    async with factory() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=await asyncio.to_thread(hash_password, password),
                    role="admin",
                )
            )
            action = "created"
        elif user.role != "admin" or not await asyncio.to_thread(
            verify_password, password, user.password_hash
        ):
            user.role = "admin"
            user.password_hash = await asyncio.to_thread(hash_password, password)
            action = "updated"
        else:
            action = "unchanged"
    log.info("admin_seeded", action=action)


async def seed_demo_bot(
    settings: Settings, factory: async_sessionmaker[AsyncSession], redis: Redis
) -> None:
    if not settings.demo_seed:
        return
    flow = FlowGraph.model_validate_json(settings.demo_flow_path.read_text(encoding="utf-8"))
    result = validate_flow(flow, KNOWN_TOOLS)
    if not result.ok:
        # A broken flow file is a bug in the repo; fail the start, not the first call.
        raise RuntimeError(f"{settings.demo_flow_path} is invalid: {result.errors}")

    try:
        async with factory() as db, db.begin():
            # The row lock serialises replicas starting together: the second one sees the
            # first one's version and finds nothing to do.
            bot = await db.get(Bot, settings.demo_bot_id, with_for_update=True)
            if bot is None:
                bot = Bot(
                    id=settings.demo_bot_id,
                    name=DEMO_BOT_NAME,
                    provider=DEMO_PROVIDER,
                    model=DEMO_MODEL,
                    draft_flow=flow.model_dump(mode="json"),
                    draft_updated_at=func.now(),
                )
                db.add(bot)
                await db.flush()
                version = await make_live(db, bot, flow, SEEDED_NOTE, None)
                action = "created"
            else:
                live = (
                    await db.get(BotVersion, bot.live_version_id) if bot.live_version_id else None
                )
                if live is not None and FlowGraph.model_validate(live.flow).same_logic(flow):
                    log.info("demo_bot_seeded", action="unchanged", version=live.version)
                    return
                reason = _not_the_seeds(bot, live)
                if reason is not None:
                    log.warning("demo_bot_seed_skipped", bot_id=str(bot.id), reason=reason)
                    return
                bot.draft_flow = flow.model_dump(mode="json")
                bot.draft_updated_at = func.now()
                version = await make_live(db, bot, flow, REPUBLISHED_NOTE, None)
                action = "republished"
    except IntegrityError:
        # Another replica created the bot between our read and insert.
        log.info("demo_bot_seeded", action="created_elsewhere")
        return
    await announce_live(redis, bot, version)
    log.info("demo_bot_seeded", action=action, bot_id=str(bot.id), version=version.version)


def _not_the_seeds(bot: Bot, live: BotVersion | None) -> str | None:
    """Why the seed must not republish ``bot``, or ``None`` if it may."""
    if live is None:
        return "the bot has no live version"
    if live.created_by is not None:
        return f"v{live.version} was published by a person"
    draft = None if bot.draft_flow is None else FlowGraph.model_validate(bot.draft_flow)
    if draft is not None and not draft.same_logic(FlowGraph.model_validate(live.flow)):
        return "the draft has unpublished edits"
    return None
