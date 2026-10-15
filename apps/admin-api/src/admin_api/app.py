"""FastAPI app of the admin API: health check, auth, access codes, call tickets, bots and
drafts so far; versions and call logs come with their tickets."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import access_codes, auth, bots, calls, errors
from admin_api.seed import seed_admin
from admin_api.sessions import SessionStore
from rozmova_core.db import create_session_factory
from rozmova_core.settings import Settings


def create_app(settings: Settings, engine: AsyncEngine, redis: Redis) -> FastAPI:
    """The app owns ``engine`` and ``redis`` and closes them on shutdown."""
    factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await seed_admin(settings, factory)
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Rozmova admin API", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = factory
    app.state.redis = redis
    app.state.sessions = SessionStore(redis, settings.admin_session_ttl_seconds)
    errors.install(app)
    app.include_router(auth.router)
    app.include_router(access_codes.login_router)
    app.include_router(access_codes.router)
    app.include_router(calls.router)
    app.include_router(bots.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
