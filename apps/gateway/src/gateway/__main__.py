"""``python -m gateway``: the gateway with the static demo bot.

Shutdown is ours, not uvicorn's. On SIGTERM uvicorn at once fails every open WebSocket
with 1012 (mid-reply, no ``session_end``) and then re-raises the signal, so the process
dies by SIGTERM and Railway reports "Deploy Crashed". ``GatewayServer`` takes the first
SIGTERM or SIGINT itself: it drains the calls (``Calls.drain``), then lets uvicorn stop
with no WebSockets left and exit 0. A second signal stops at once, the uvicorn way.
"""

import asyncio
import os
from types import FrameType

import structlog
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.app import create_app
from gateway.bots import PublishedBotLoader
from gateway.calls import Calls
from rozmova_core.credentials import CredentialCipher
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.log import configure_logging
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_core.telemetry import init_telemetry, shutdown_telemetry
from rozmova_engine.providers.keys import DemoKeySettings, Providers
from rozmova_engine.rag.answer import KnowledgeBase, KnowledgeSearch, UnavailableKnowledgeBase
from rozmova_engine.rag.embeddings import OpenAIEmbedder

log = structlog.get_logger(__name__)

# SDD: ping every 15 s; a client that misses a pong for as long is disconnected.
WS_PING_INTERVAL = 15.0
WS_PING_TIMEOUT = 15.0
# Railway kills the old instance 60 s after SIGTERM (RAILWAY_DEPLOYMENT_DRAINING_SECONDS);
# turns get 50 s, which leaves time to flush, close and exit. Turns time out at 30 s.
DRAIN_SECONDS = 50.0


class GatewayServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, calls: Calls) -> None:
        super().__init__(config)
        self._calls = calls
        self._drain_requested = False
        self._drain: asyncio.Task[None] | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # A signal handler: only set a flag, ``on_tick`` acts on it in the event loop.
        if self._drain_requested:
            super().handle_exit(sig, frame)
            return
        self._drain_requested = True

    async def on_tick(self, counter: int) -> bool:
        """Called by uvicorn's main loop every 0.1 s; ``True`` stops the server."""
        if self._drain_requested and self._drain is None:
            self._drain = asyncio.create_task(self._calls.drain(DRAIN_SECONDS), name="drain")
        if self._drain is not None and self._drain.done():
            return True
        return await super().on_tick(counter)


def serve(app: FastAPI, calls: Calls, *, host: str, port: int) -> GatewayServer:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        ws_ping_interval=WS_PING_INTERVAL,
        ws_ping_timeout=WS_PING_TIMEOUT,
        timeout_graceful_shutdown=5,
        log_config=None,
    )
    return GatewayServer(config, calls)


def knowledge_base(
    settings: Settings, demo: DemoKeySettings, engine: AsyncEngine
) -> KnowledgeSearch:
    """Query embeddings use ``OPENAI_API_KEY``, or the demo LLM key when it is an OpenAI key
    (one key serves both on the demo)."""
    key = settings.openai_api_key or (demo.api_key if demo.provider == "openai" else None)
    if key is None:
        log.warning("kb_unavailable", reason="no OpenAI key for query embeddings")
        return UnavailableKnowledgeBase("no OpenAI key for query embeddings")
    embedder = OpenAIEmbedder.create(key, settings.embedding_model)
    return KnowledgeBase(create_session_factory(engine), embedder)


def build() -> GatewayServer:
    settings = Settings()
    configure_logging(settings)
    # Traces only with OTEL_EXPORTER_OTLP_ENDPOINT; without it the tracer stays a no-op.
    init_telemetry(settings)
    redis = create_redis(settings)
    demo = DemoKeySettings()
    # One pool for bots and the knowledge base; the loader disposes it on shutdown.
    engine = create_engine(settings)
    providers = Providers.from_settings(demo, CredentialCipher.from_settings(settings))
    loader = PublishedBotLoader(redis, engine, providers)
    calls = Calls()
    app = create_app(loader, redis, settings, calls, knowledge_base(settings, demo, engine))
    # A container service listens on all interfaces.
    return serve(app, calls, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))  # noqa: S104


def main() -> None:
    try:
        build().run()
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
