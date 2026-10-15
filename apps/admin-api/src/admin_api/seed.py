"""Data admin-api creates on every start, so a new environment works without manual SQL.

- The admin user from ``ROZMOVA_ADMIN_EMAIL`` / ``ROZMOVA_ADMIN_PASSWORD``; changing the
  variable is how the admin password is reset.
- The demo bot (ROZ-54): the SDD's Hvylia Mobile flow from ``flows/hvylia-demo.json``,
  saved as the draft and published as v1, under the fixed ``ROZMOVA_DEMO_BOT_ID`` the
  smoke test and the public widget call. Once the bot exists it is never touched again,
  so edits made in the admin survive restarts.
"""

import asyncio

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import normalize_email
from admin_api.bots import KNOWN_TOOLS
from admin_api.passwords import hash_password, verify_password
from admin_api.publishing import announce_live, make_live
from rozmova_contracts.flow import FlowGraph
from rozmova_core.models import Bot, User
from rozmova_core.settings import Settings
from rozmova_engine.flow import validate_flow

DEMO_BOT_NAME = "Hvylia Mobile demo"
DEMO_PROVIDER = "openai"
DEMO_MODEL = "gpt-4.1-mini"

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

    async with factory() as db, db.begin():
        if await db.get(Bot, settings.demo_bot_id) is not None:
            log.info("demo_bot_seeded", action="unchanged")
            return
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
        version = await make_live(db, bot, flow, "seeded demo flow", None)
    await announce_live(redis, bot, version)
    log.info("demo_bot_seeded", action="created", bot_id=str(bot.id), version=version.version)
