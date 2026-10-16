"""FastAPI app of the admin API: health check, auth, access codes, call tickets, bots,
drafts, publish and rollback so far; version history and call logs come with their tickets.
It also serves the built admin-web: the public demo chat under /demo/ (ROZ-68), and
persists the gateway's call events into the call log in the background (ROZ-62)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import access_codes, auth, bots, calls, errors, publishing
from admin_api.call_log import start_call_log, stop_call_log
from admin_api.kb import start_ingest, stop_ingest
from admin_api.seed import seed_admin, seed_demo_bot
from admin_api.sessions import SessionStore
from rozmova_core.db import create_session_factory
from rozmova_core.settings import Settings


def create_app(settings: Settings, engine: AsyncEngine, redis: Redis) -> FastAPI:
    """The app owns ``engine`` and ``redis`` and closes them on shutdown."""
    factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ingest = call_log = None
        try:
            await seed_admin(settings, factory)
            await seed_demo_bot(settings, factory, redis)
            ingest = start_ingest(settings, engine)
            call_log = start_call_log(redis, factory)
            yield
        finally:
            await stop_call_log(call_log)
            await stop_ingest(ingest)
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
    app.include_router(publishing.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.admin_web_dir is not None:
        app.mount("/demo", StaticFiles(directory=settings.admin_web_dir, html=True), name="demo")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/demo/")

    return app
