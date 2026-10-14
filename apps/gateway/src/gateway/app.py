"""FastAPI app of the dialogue gateway: ``WS /ws/calls/{bot_id}`` and a health check.

The endpoint wires one ``Session`` per connection: the bot from the ``BotLoader``, the
mock subscriber tools over one process-wide billing, the tool loop and the flow executor.
Run it with ``python -m gateway`` (uvicorn with WebSocket ping every 15 s).

Not here yet: ticket verification and the debug flag from the ticket (ROZ-50; ``debug``
is a query parameter for now), session state in Redis and resume (ROZ-59), the knowledge
base for ``kb_answer`` nodes (ROZ-43).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket

from gateway.bots import BotLoader
from gateway.session import Session
from rozmova_contracts import protocol
from rozmova_core.log import log_context
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

# Close code for "no such bot": the 4xxx range is for applications.
BOT_NOT_FOUND = 4404


def create_app(loader: BotLoader) -> FastAPI:
    billing = MockBilling(demo_subscribers())
    registry = ToolRegistry(subscriber_tools(billing))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        provider = getattr(loader, "provider", None)
        if provider is not None and hasattr(provider, "aclose"):
            await provider.aclose()

    app = FastAPI(title="Rozmova gateway", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws/calls/{bot_id}")
    async def call(
        websocket: WebSocket, bot_id: UUID, call_id: UUID | None = None, debug: bool = False
    ) -> None:
        bot = await loader.load(bot_id)
        if bot is None:
            await websocket.close(code=BOT_NOT_FOUND)
            return
        await websocket.accept()

        state = SessionState(call_id=call_id or uuid4(), bot_id=bot_id, bot_version=bot.bot_version)
        # Every caller is the demo subscriber until tickets carry a subscriber (ROZ-50).
        ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
        session: Session

        # The engine emits through the session's outbox; the session exists only below.
        async def emit(event: protocol.ProtocolMessage) -> None:
            await session.emit(event)

        loop = ToolLoop(bot.provider, bot.model, registry, ctx, emit)
        executor = FlowExecutor(bot.flow, loop, registry, ctx, emit)
        session = Session(websocket, executor, state, debug=debug)

        bound = log_context(
            call_id=str(state.call_id), bot_id=str(bot_id), bot_version=bot.bot_version
        )
        with bound:
            await session.run()

    return app
