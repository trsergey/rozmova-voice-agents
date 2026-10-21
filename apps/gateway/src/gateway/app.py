"""FastAPI app of the dialogue gateway: ``WS /ws/calls/{bot_id}`` and a health check.

The endpoint admits a connection only with a valid ticket (``admit``), resumes the call's
state from Redis if it is there (FR-21), then wires one ``Session``: the bot from the
``BotLoader``, the mock subscriber tools over one process-wide billing, the tool loop and
the flow executor. Run it with ``python -m gateway`` (uvicorn with WebSocket ping every
15 s). While the process drains for a shutdown (``Calls``), new calls get HTTP 503.
Demo limits are per user from the ticket (``RateLimiter``); a user with all their calls
open gets ``session_end{limit}`` at once.

A new call runs on the bot's live version; a resumed call keeps the version it started on
(``bot_version`` in its saved state), even if a newer one was published meanwhile.
``?draft=true`` runs the draft instead, for debug tickets only (the admin's test console).

Every call is recorded to the Redis Stream ``call-events`` through one ``CallEventWriter``
(ROZ-52); admin-api persists it.

``kb`` answers ``kb_answer`` nodes and backs the ``search_knowledge_base`` tool (ROZ-43).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from fastapi import FastAPI, Response, WebSocket
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from gateway.bots import BotLoader, BotRuntime, BotUnavailableError
from gateway.call_events import CallEventWriter, CallRecorder
from gateway.calls import Calls
from gateway.limits import RateLimiter
from gateway.metrics import ACTIVE_SESSIONS, tracer
from gateway.session import Session
from gateway.state_store import SessionStore
from rozmova_contracts import protocol
from rozmova_core import tickets
from rozmova_core.log import log_context
from rozmova_core.settings import Settings
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.rag.answer import KnowledgeBaseAnswerer, KnowledgeSearch, knowledge_tools
from rozmova_engine.session import SessionState, SessionStateError
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
BOT_UNAVAILABLE = 4503  # the database or the provider is down; retry with a new ticket


async def refuse(websocket: WebSocket, code: int) -> None:
    """Close with an application code the browser can see.

    Closing before ``accept()`` makes uvicorn answer the handshake with HTTP 403, and a
    browser then only sees code 1006 with no reason. Accepting first and closing at once
    delivers ``code``, so the client can tell "get a new ticket" from a network failure.
    Nothing has run for the connection by then: no session, no LLM call.
    """
    await websocket.accept()
    await websocket.close(code=code)


def client_ip(websocket: WebSocket) -> str | None:
    """For logs only, never for limits: the leftmost ``X-Forwarded-For`` value.

    Railway's edge overwrites that header, so the leftmost value is the client; the
    proxy ranges are not documented, so the address is not trusted for anything else.
    """
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return websocket.client.host if websocket.client else None


def create_app(
    loader: BotLoader,
    redis: Redis,
    settings: Settings,
    calls: Calls | None = None,
    kb: KnowledgeSearch | None = None,
) -> FastAPI:
    """The app owns ``redis`` and ``kb`` and closes them on shutdown; ``calls`` is for
    draining it. Without ``kb`` a ``kb_answer`` node is a flow error (handoff)."""
    calls = calls if calls is not None else Calls()
    if settings.ticket_secret is None:
        raise RuntimeError("ROZMOVA_TICKET_SECRET is required by the gateway")
    secret = settings.ticket_secret.get_secret_value()
    allowed_origins = frozenset(settings.allowed_origins)

    limiter = RateLimiter(redis, settings)

    def call_tools() -> ToolRegistry:
        """Tools for one call. The mock billing is per call, so every visitor starts from
        the same fictional account and one visitor's activation never shows up in another's
        call (ROZ-45). A reconnect gets a fresh one too: the demo has no billing to resume."""
        billing = MockBilling(demo_subscribers())
        return ToolRegistry([*subscriber_tools(billing), *(knowledge_tools(kb) if kb else [])])

    events = CallEventWriter(redis)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        watch = asyncio.create_task(loader.watch(), name="bot-cache-watch")
        events.start()
        try:
            yield
        finally:
            watch.cancel()
            await asyncio.wait([watch])
            # After the calls have drained: their last events are still queued.
            await events.aclose()
            await loader.aclose()
            await redis.aclose()
            close_kb = getattr(kb, "aclose", None)
            if close_kb is not None:
                await close_kb()

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

    async def resume(store: SessionStore, bot_id: UUID) -> SessionState | None:
        """The call's saved state, now owned by this connection; ``None`` for a new call.

        State that cannot be used (another format version after a deploy, or another bot)
        is replaced by a new call rather than refused: the subscriber loses the history,
        not the call.
        """
        try:
            state = await store.claim()
        except SessionStateError as exc:
            log.warning("session_state_unreadable", error=str(exc))
            return None
        if state is not None and state.bot_id != bot_id:
            log.warning("session_state_other_bot", stored_bot_id=str(state.bot_id))
            return None
        return state

    app = FastAPI(title="Rozmova gateway", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.websocket("/ws/calls/{bot_id}")
    async def call(
        websocket: WebSocket,
        bot_id: UUID,
        call_id: UUID | None = None,
        ticket: str | None = None,
        draft: bool = False,
    ) -> None:
        if calls.draining:
            # Before the upgrade, so the client knows to retry (the new instance answers).
            await websocket.send_denial_response(Response(status_code=503))
            return
        grant = await admit(websocket, bot_id, call_id, ticket)
        if grant is None:
            await refuse(websocket, UNAUTHORIZED)
            return
        if draft and not grant.debug:
            log.warning("call_refused", reason="draft_without_debug", user_id=grant.user_id)
            await refuse(websocket, UNAUTHORIZED)
            return
        try:
            bot = await loader.load(bot_id, draft=draft)
        except BotUnavailableError:
            await refuse(websocket, BOT_UNAVAILABLE)
            return
        if bot is None:
            log.warning("call_refused", reason="bot_not_found", bot_id=str(bot_id))
            await refuse(websocket, BOT_NOT_FOUND)
            return
        store = SessionStore(redis, grant.call_id)
        if not await limiter.open_call(grant.user_id, store.owner):
            log.warning("call_refused", reason="too_many_calls", user_id=grant.user_id)
            await websocket.accept()
            await websocket.send_text(protocol.SessionEnd(reason="limit").model_dump_json())
            await websocket.close()
            return
        try:
            await run_call(websocket, bot_id, bot, grant, store)
        finally:
            await limiter.close_call(grant.user_id, store.owner)

    async def run_call(
        websocket: WebSocket,
        bot_id: UUID,
        bot: BotRuntime,
        grant: tickets.Ticket,
        store: SessionStore,
    ) -> None:
        state = await resume(store, bot_id)
        if state is not None and state.bot_version != bot.bot_version:
            # The call started on another version: keep it, the flow must not change mid-call.
            try:
                pinned = (
                    await loader.load(bot_id, version=state.bot_version)
                    if state.bot_version is not None
                    else None
                )
            except BotUnavailableError:
                await refuse(websocket, BOT_UNAVAILABLE)
                return
            if pinned is None:
                log.warning("session_version_gone", stored_version=state.bot_version)
                state = None
            else:
                bot = pinned
        resumed = state is not None
        if state is None:
            state = SessionState(call_id=grant.call_id, bot_id=bot_id, bot_version=bot.bot_version)
        await websocket.accept()

        # Every caller is the demo subscriber; the ticket identifies the viewer, not a
        # subscriber.
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        session: Session

        # The engine emits through the session's outbox; the session exists only below.
        async def emit(event: protocol.ProtocolMessage) -> None:
            await session.emit(event)

        registry = call_tools()
        loop = ToolLoop(
            bot.provider, bot.model, registry, ctx, emit, provider_name=bot.provider_name
        )
        answerer = KnowledgeBaseAnswerer(kb, loop, emit) if kb is not None else None
        executor = FlowExecutor(bot.flow, loop, registry, ctx, emit, kb=answerer)
        session = Session(
            websocket,
            executor,
            state,
            debug=grant.debug,
            store=store,
            resumed=resumed,
            limits=limiter.for_call(grant.user_id),
            recorder=CallRecorder(
                events,
                call_id=state.call_id,
                bot_id=bot_id,
                user_id=grant.user_id,
                debug=grant.debug,
            ),
        )

        bound = log_context(
            call_id=str(state.call_id),
            bot_id=str(bot_id),
            bot_version=bot.bot_version,
            user_id=grant.user_id,
        )
        span_attributes: dict[str, str | int | bool] = {
            "call.id": str(state.call_id),
            "bot.id": str(bot_id),
            "bot.version": bot.bot_version if bot.bot_version is not None else -1,
            "call.resumed": resumed,
        }
        active = ACTIVE_SESSIONS.labels(str(bot_id))
        with (
            bound,
            calls.track(session),
            tracer.start_as_current_span("call", attributes=span_attributes),
        ):
            log.info(
                "session_opened", resumed=resumed, turn=state.turn, client_ip=client_ip(websocket)
            )
            active.inc()
            try:
                await session.run()
            finally:
                active.dec()

    return app
