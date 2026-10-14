"""One WebSocket call: three tasks under one ``asyncio.TaskGroup`` (ADR 0002, SDD gateway).

- ``reader`` parses client frames with the protocol contracts; a malformed or unknown
  frame becomes an ``error`` event, not a disconnect. Utterances go to the bounded
  ``inbox``.
- ``turn_loop`` starts the call (greeting), then runs every utterance as its own
  ``current_turn`` task through the flow executor, under the 30 s turn timeout.
- ``writer`` sends events from the ``Outbox`` (bounded, coalescing; see ``outbox.py``).

Any task ends the session by raising ``SessionClosing``; the ``TaskGroup`` then cancels
the other two, and ``run`` flushes what is still queued and closes the socket with the
right code. An unexpected exception in a task closes the session with 1011 after an
``error`` event. Ping/pong is done by uvicorn (``ws_ping_interval``), not here.

Turns run one after another in this ticket; cancelling a running turn on a new
utterance (FR-4) comes with ROZ-49.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from gateway.outbox import Outbox, OutboxStalledError
from rozmova_contracts import protocol
from rozmova_engine.flow import FlowExecutor, TurnOutcome
from rozmova_engine.providers import ProviderError
from rozmova_engine.session import SessionState
from rozmova_engine.timings import TurnTimer

log = structlog.get_logger(__name__)

TURN_TIMEOUT = 30.0
INBOX_SIZE = 16
FLUSH_TIMEOUT = 2.0

# WebSocket close codes.
NORMAL = 1000
INTERNAL_ERROR = 1011
TRY_AGAIN_LATER = 1013


class Transport(Protocol):
    """The part of Starlette's ``WebSocket`` a session uses; tests pass a fake."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass
class SessionClosing(Exception):  # noqa: N818 - a control-flow signal, not an error
    """End the session: ``flush`` sends what is queued first; ``code`` closes the socket.

    ``code`` is ``None`` when the client is already gone and there is nothing to close.
    """

    code: int | None = NORMAL
    flush: bool = True


class Session:
    def __init__(
        self,
        transport: Transport,
        executor: FlowExecutor,
        state: SessionState,
        *,
        debug: bool = False,
        outbox: Outbox | None = None,
        turn_timeout: float = TURN_TIMEOUT,
    ) -> None:
        self._transport = transport
        self._executor = executor
        self.state = state
        self._debug = debug
        # Not `outbox or Outbox()`: an empty Outbox is falsy (it has __len__).
        self._outbox = outbox if outbox is not None else Outbox()
        self._inbox: asyncio.Queue[str] = asyncio.Queue(maxsize=INBOX_SIZE)
        self._turn_timeout = turn_timeout
        self._current_turn: asyncio.Task[None] | None = None

    @property
    def call_id(self) -> UUID:
        return self.state.call_id

    async def run(self) -> None:
        closing = SessionClosing(code=INTERNAL_ERROR)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._reader(), name="reader")
                tg.create_task(self._turn_loop(), name="turn_loop")
                tg.create_task(self._writer(), name="writer")
        except* SessionClosing as group:
            closing = group.exceptions[0]  # type: ignore[assignment]
        except* Exception as group:
            log.error("session_failed", exc_info=group.exceptions[0])
            self._outbox.take_all()
            await self._send_quietly(
                protocol.Error(code="internal", message="The session failed unexpectedly.")
            )
        await self._finish(closing)

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        """Queue an event for the client; debug-only events only in debug sessions."""
        if event.debug_only and not self._debug:
            return
        try:
            await self._outbox.put(event)
        except OutboxStalledError:
            log.warning("outbox_stalled")
            raise SessionClosing(code=TRY_AGAIN_LATER, flush=False) from None

    # --- tasks ---------------------------------------------------------------------

    async def _reader(self) -> None:
        while True:
            try:
                raw = await self._transport.receive_text()
            except WebSocketDisconnect:
                raise SessionClosing(code=None, flush=False) from None
            try:
                message = protocol.parse_client_message(raw)
            except ValidationError as err:
                await self.emit(_invalid_message(err))
                continue
            match message:
                case protocol.UserText(text=text):
                    await self._inbox.put(text)
                case protocol.Hangup():
                    await self.emit(protocol.SessionEnd(reason="hangup"))
                    raise SessionClosing()
                case protocol.AudioChunk():
                    await self.emit(
                        protocol.Error(
                            code="invalid_message", message="Audio is not supported yet."
                        )
                    )

    async def _turn_loop(self) -> None:
        try:
            await self._start_call()
            while True:
                text = await self._inbox.get()
                self._current_turn = asyncio.create_task(self._run_turn(text), name="turn")
                await self._current_turn
        finally:
            # turn_loop is being cancelled or failed: never leave the turn running behind.
            if self._current_turn is not None and not self._current_turn.done():
                self._current_turn.cancel()
                with suppress(asyncio.CancelledError):
                    await self._current_turn

    async def _writer(self) -> None:
        while True:
            event = await self._outbox.peek()
            try:
                await self._transport.send_text(event.model_dump_json())
            except WebSocketDisconnect:
                raise SessionClosing(code=None, flush=False) from None
            await self._outbox.pop()

    # --- turns ---------------------------------------------------------------------

    async def _start_call(self) -> None:
        await self.emit(
            protocol.SessionStarted(call_id=self.call_id, bot_version=self.state.bot_version)
        )
        timer = TurnTimer()
        outcome = await self._executor.start(self.state, timer)
        await self._end_turn(outcome, timer)

    async def _run_turn(self, text: str) -> None:
        """One subscriber turn; failures become ``error`` events, the session goes on."""
        timer = TurnTimer()
        try:
            async with asyncio.timeout(self._turn_timeout):
                outcome = await self._executor.handle(self.state, text, timer)
        except SessionClosing:
            raise
        except TimeoutError:
            log.warning("turn_timeout", turn_id=self.state.turn)
            await self.emit(
                protocol.Error(
                    code="turn_timeout", message="The turn took too long.", retryable=True
                )
            )
            return
        except ProviderError as err:
            log.warning("llm_unavailable", error=str(err), retryable=err.retryable)
            await self.emit(
                protocol.Error(
                    code="llm_unavailable",
                    message="The language model is not available.",
                    retryable=err.retryable,
                )
            )
            return
        except Exception:
            log.exception("turn_failed", turn_id=self.state.turn)
            await self.emit(protocol.Error(code="internal", message="The turn failed."))
            return
        await self._end_turn(outcome, timer)

    async def _end_turn(self, outcome: TurnOutcome, timer: TurnTimer) -> None:
        await self.emit(
            protocol.TurnEnd(
                turn_id=self.state.turn, timings=timer.to_timings(), sources=outcome.sources
            )
        )
        if outcome.ended is not None:
            await self.emit(protocol.SessionEnd(reason=outcome.ended))
            raise SessionClosing()

    # --- closing -------------------------------------------------------------------

    async def _finish(self, closing: SessionClosing) -> None:
        if closing.flush:
            for event in self._outbox.take_all():
                await self._send_quietly(event)
        if closing.code is not None:
            with suppress(Exception):
                await self._transport.close(code=closing.code)
        log.info("session_closed", code=closing.code)

    async def _send_quietly(self, event: protocol.ProtocolMessage) -> None:
        """Best-effort send while closing: a gone or stuck client must not block it."""
        with suppress(Exception):
            async with asyncio.timeout(FLUSH_TIMEOUT):
                await self._transport.send_text(event.model_dump_json())


def _invalid_message(err: ValidationError) -> protocol.Error:
    first = err.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "frame"
    message = f"Invalid message: {where}: {first['msg']}"
    return protocol.Error(code="invalid_message", message=message)
