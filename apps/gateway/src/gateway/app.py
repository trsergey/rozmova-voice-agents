"""FastAPI app of the dialogue gateway: ``WS /ws/calls/{bot_id}`` and a health check.

The endpoint admits a connection only with a valid ticket (``admit``), then wires one
``Session``: the bot from the ``BotLoader``, the mock subscriber tools over one process-wide
billing, the tool loop and the flow executor. Run it with ``python -m gateway`` (uvicorn
with WebSocket ping every 15 s).

Not here yet: session state in Redis and resume (ROZ-59), the knowledge base for
``kb_answer`` nodes (ROZ-43).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from fastapi import FastAPI, WebSocket
from redis.asyncio import Redis

from gateway.bots import BotLoader
from gateway.session import Session
from rozmova_contracts import protocol
from rozmova_core import tickets
from rozmova_core.log import log_context
from rozmova_core.settings import Settings
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.session import SessionState
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import (
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    ToolContext,
    ToolRegistry,
    demo_subscribers,
    subscriber_tools,
)

log = structlog.get_logger(__name__)

# Close codes in the 4xxx range are for applications.
UNAUTHORIZED = 4401
BOT_NOT_FOUND = 4404


async def refuse(websocket: WebSocket, code: int) -> None:
    """Close with an application code the browser can see.

    Closing before ``accept()`` makes uvicorn answer the handshake with HTTP 403, and a
    browser then only sees code 1006 with no reason. Accepting first and closing at once
    delivers ``code``, so the client can tell "get a new ticket" from a network failure.
    Nothing has run for the connection by then: no session, no LLM call.
    """
    await websocket.accept()
    await websocket.close(code=code)


def create_app(loader: BotLoader, redis: Redis, settings: Settings) -> FastAPI:
    """The app owns ``redis`` and closes it on shutdown."""
    if settings.ticket_secret is None:
        raise RuntimeError("ROZMOVA_TICKET_SECRET is required by the gateway")
    secret = settings.ticket_secret.get_secret_value()
    allowed_origins = frozenset(settings.allowed_origins)

    billing = MockBilling(demo_subscribers())
    registry = ToolRegistry(subscriber_tools(billing))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await redis.aclose()
        provider = getattr(loader, "provider", None)
        if provider is not None and hasattr(provider, "aclose"):
            await provider.aclose()

    async def admit(
        websocket: WebSocket, bot_id: UUID, call_id: UUID | None, token: str | None
    ) -> tickets.Ticket | None:
        """The ticket if the connection may start a call, else ``None`` (reason in the log).

        The ``Origin`` check stops other sites from using a viewer's browser; clients
        without the header are not browsers and are held by the ticket alone. Single use
        is checked last, so a ticket that fails another check is not burnt.
        """
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            log.warning("call_refused", reason="origin", origin=origin)
            return None
        if token is None or call_id is None:
            log.warning("call_refused", reason="no_ticket")
            return None
        try:
            ticket = tickets.verify(token, secret)
        except tickets.TicketError as exc:
            log.warning("call_refused", reason=str(exc))
            return None
        if ticket.bot_id != bot_id or ticket.call_id != call_id:
            log.warning("call_refused", reason="ticket_mismatch", ticket_id=str(ticket.id))
            return None
        if not await tickets.consume(redis, ticket.id):
            log.warning("call_refused", reason="ticket_used", ticket_id=str(ticket.id))
            return None
        return ticket

    app = FastAPI(title="Rozmova gateway", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws/calls/{bot_id}")
    async def call(
        websocket: WebSocket,
        bot_id: UUID,
        call_id: UUID | None = None,
        ticket: str | None = None,
    ) -> None:
        grant = await admit(websocket, bot_id, call_id, ticket)
        if grant is None:
            await refuse(websocket, UNAUTHORIZED)
            return
        bot = await loader.load(bot_id)
        if bot is None:
            await refuse(websocket, BOT_NOT_FOUND)
            return
        await websocket.accept()

        state = SessionState(call_id=grant.call_id, bot_id=bot_id, bot_version=bot.bot_version)
        # Every caller is the demo subscriber; the ticket identifies the viewer, not a
        # subscriber.
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        session: Session

        # The engine emits through the session's outbox; the session exists only below.
        async def emit(event: protocol.ProtocolMessage) -> None:
            await session.emit(event)

        loop = ToolLoop(bot.provider, bot.model, registry, ctx, emit)
        executor = FlowExecutor(bot.flow, loop, registry, ctx, emit)
        session = Session(websocket, executor, state, debug=grant.debug)

        bound = log_context(
            call_id=str(state.call_id),
            bot_id=str(bot_id),
            bot_version=bot.bot_version,
            user_id=grant.user_id,
        )
        with bound:
            await session.run()

    return app
