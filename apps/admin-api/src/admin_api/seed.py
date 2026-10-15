"""Create or update the admin user from ``ROZMOVA_ADMIN_EMAIL`` / ``ROZMOVA_ADMIN_PASSWORD``.

Runs on every admin-api start: a new environment gets its admin without manual SQL, and
changing the variable is how the admin password is reset.
"""

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import normalize_email
from admin_api.passwords import hash_password, verify_password
from rozmova_core.models import User
from rozmova_core.settings import Settings

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
