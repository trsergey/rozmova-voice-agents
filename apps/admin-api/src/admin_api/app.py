"""FastAPI app of the admin API: health check, auth, access codes, call tickets, bots,
drafts, publish and rollback, provider keys, the knowledge base; version history and call
logs come with their tickets. It also serves the built admin-web (``spa``), and persists the
gateway's call events into the call log in the background (ROZ-62)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import access_codes, auth, bots, calls, credentials, errors, kb, publishing, spa
from admin_api.call_log import start_call_log, stop_call_log
from admin_api.seed import seed_admin, seed_demo_bot
from admin_api.sessions import SessionStore
from rozmova_core.credentials import CredentialCipher
from rozmova_core.db import create_session_factory
from rozmova_core.settings import Settings


def create_app(settings: Settings, engine: AsyncEngine, redis: Redis) -> FastAPI:
    """The app owns ``engine`` and ``redis`` and closes them on shutdown."""
    factory = create_session_factory(engine)
    indexer = kb.KbIndexer(settings, engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        call_log = None
        try:
            await seed_admin(settings, factory)
            await seed_demo_bot(settings, factory, redis)
            if settings.kb_ingest_on_start:
                indexer.start()
            call_log = start_call_log(redis, factory)
            yield
        finally:
            await stop_call_log(call_log)
            await indexer.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Rozmova admin API", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = factory
    app.state.redis = redis
    app.state.kb = indexer
    app.state.cipher = CredentialCipher.from_settings(settings)
    app.state.sessions = SessionStore(redis, settings.admin_session_ttl_seconds)
    errors.install(app)
    app.include_router(auth.router)
    app.include_router(access_codes.login_router)
    app.include_router(access_codes.router)
    app.include_router(calls.router)
    app.include_router(bots.router)
    app.include_router(publishing.router)
    app.include_router(credentials.router)
    app.include_router(kb.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.admin_web_dir is not None:
        spa.install(app, settings.admin_web_dir)

    return app
