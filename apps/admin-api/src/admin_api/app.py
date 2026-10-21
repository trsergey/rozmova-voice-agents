"""FastAPI app of the admin API: health check, auth, access codes, call tickets, bots,
drafts, evals over a draft, publish and rollback, version history and diff, tool usages,
provider keys, the knowledge base, the call log and the ops summary. It also serves the
built admin-web (``spa``), and persists the gateway's call events into the call log in the
background (ROZ-62)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import (
    access_codes,
    auth,
    bots,
    call_history,
    calls,
    credentials,
    demo_reset,
    errors,
    evals,
    kb,
    ops,
    publishing,
    spa,
    usages,
)
from admin_api.call_log import start_call_log, stop_call_log
from admin_api.seed import seed_admin, seed_demo_bot
from admin_api.sessions import SessionStore
from rozmova_core.credentials import CredentialCipher
from rozmova_core.db import create_session_factory
from rozmova_core.settings import Settings


def create_app(
    settings: Settings,
    engine: AsyncEngine,
    redis: Redis,
    *,
    eval_runtime: evals.EvalRuntime | None = None,
) -> FastAPI:
    """The app owns ``engine`` and ``redis`` and closes them on shutdown. ``eval_runtime``
    replaces the LLM clients and knowledge base of eval runs (tests script them)."""
    factory = create_session_factory(engine)
    indexer = kb.KbIndexer(settings, engine)
    runtime = eval_runtime or evals.default_runtime(settings, factory)
    eval_jobs = evals.EvalJobs(settings.evals_path, redis, factory, runtime)

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
            await eval_jobs.aclose()
            await indexer.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="Rozmova admin API", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = factory
    app.state.redis = redis
    app.state.kb = indexer
    app.state.evals = eval_jobs
    app.state.ops_cache = ops.SummaryCache()
    app.state.cipher = CredentialCipher.from_settings(settings)
    app.state.sessions = SessionStore(redis, settings.admin_session_ttl_seconds)
    errors.install(app)
    app.include_router(auth.router)
    app.include_router(access_codes.login_router)
    app.include_router(access_codes.router)
    app.include_router(calls.router)
    app.include_router(call_history.router)
    app.include_router(bots.router)
    app.include_router(bots.providers_router)
    app.include_router(publishing.router)
    app.include_router(evals.router)
    app.include_router(usages.router)
    app.include_router(credentials.router)
    app.include_router(kb.router)
    app.include_router(ops.router)
    app.include_router(demo_reset.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.admin_web_dir is not None:
        spa.install(app, settings.admin_web_dir)

    return app
