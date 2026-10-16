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

A new utterance while a turn is running cancels that turn (FR-4): the client gets
``turn_cancelled``, the engine keeps the partial reply as ``interrupted`` and finishes a
started side-effect tool under ``shield`` before the cancellation completes.

The state goes to Redis (``SessionStore``, FR-21) after every turn, before its
``turn_end``: a client that saw ``turn_end`` and then dropped finds that turn on resume.
When the session closes, the state is saved again before the socket is closed (a turn
cut off by a disconnect is in it as ``interrupted``), or dropped if the call is over. A
resumed session starts with ``session_started{resumed: true, history}`` and no greeting.

Every event also goes to the call's ``CallRecorder`` (``call_events.py``), debug-only ones
included, together with the subscriber's utterances and how the connection closed.

A session can be asked to stop (``_request_stop``): it starts no new turns, lets the
running one finish until a deadline, then sends ``session_end`` with the reason.

- ``drain`` (graceful shutdown on deploy, FR-21): reason ``shutdown``, close 1012; the
  state stays in Redis, so the client reconnects to the new instance and goes on.
- Demo limits (``CallLimits``): after the last allowed turn or when the call's time is
  up, reason ``limit``; the call is over. Over the per-minute message rate an utterance
  is refused with ``error{rate_limited}`` and the call goes on.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import ValidationError
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect

from gateway.call_events import CallRecorder
from gateway.limits import CallLimits
from gateway.outbox import Outbox, OutboxStalledError
from gateway.state_store import SessionStore
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
SERVICE_RESTART = 1012
TRY_AGAIN_LATER = 1013
SUPERSEDED = 4409  # the same call_id was resumed over another connection


class Transport(Protocol):
    """The part of Starlette's ``WebSocket`` a session uses; tests pass a fake."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass
class SessionClosing(Exception):  # noqa: N818 - a control-flow signal, not an error
    """End the session: ``flush`` sends what is queued first; ``code`` closes the socket.

    ``code`` is ``None`` when the client is already gone and there is nothing to close.
    ``keep_state`` is ``False`` when the call is over (hangup, end of flow, handoff): its
    state is dropped and the call cannot be resumed.
    """

    code: int | None = NORMAL
    flush: bool = True
    keep_state: bool = True


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
        store: SessionStore | None = None,
        resumed: bool = False,
        limits: CallLimits | None = None,
        recorder: CallRecorder | None = None,
    ) -> None:
        """``store`` is ``None`` when the state is not persisted, ``limits`` when the call
        is not limited (tests, evals), ``recorder`` when the call is not recorded."""
        self._transport = transport
        self._executor = executor
        self.state = state
        self._store = store
        self._resumed = resumed
        self._limits = limits
        self._recorder = recorder
        self._debug = debug
        # Not `outbox or Outbox()`: an empty Outbox is falsy (it has __len__).
        self._outbox = outbox if outbox is not None else Outbox()
        self._inbox: asyncio.Queue[str] = asyncio.Queue(maxsize=INBOX_SIZE)
        self._turn_timeout = turn_timeout
        self._current_turn: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._stop_reason: protocol.SessionEndReason = "shutdown"
        self._stop_deadline = 0.0

    @property
    def call_id(self) -> UUID:
        return self.state.call_id

    async def run(self) -> None:
        closing = SessionClosing(code=INTERNAL_ERROR)
        time_is_up = self._schedule_time_limit()
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
        finally:
            if time_is_up is not None:
                time_is_up.cancel()
        await self._finish(closing)

    def drain(self, deadline: float) -> None:
        """End the call for a shutdown once the running turn is done or ``deadline`` passes.

        ``deadline`` is on the event loop clock (``loop.time()``).
        """
        self._request_stop("shutdown", deadline)

    def _request_stop(self, reason: protocol.SessionEndReason, deadline: float) -> None:
        """Stop after the running turn; the first request wins."""
        if self._stop.is_set():
            return
        self._stop_reason = reason
        self._stop_deadline = deadline
        self._stop.set()

    async def emit(self, event: protocol.ProtocolMessage) -> None:
        """Record the event and queue it for the client; debug-only events reach only
        debug sessions."""
        if self._recorder is not None:
            self._recorder.observe(event)
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
                    if self._limits is not None and not await self._limits.allow_message():
                        await self.emit(
                            protocol.Error(
                                code="rate_limited",
                                message="Too many messages. Wait a minute and try again.",
                                retryable=True,
                            )
                        )
                        continue
                    await self._inbox.put(text)
                case protocol.Hangup():
                    await self.emit(protocol.SessionEnd(reason="hangup"))
                    raise SessionClosing(keep_state=False)
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
                text = await self._next_utterance()
                await self._cancel_current_turn()
                self._current_turn = asyncio.create_task(self._run_turn(text), name="turn")
        finally:
            # The session is closing: never leave a turn running behind it.
            await self._cancel_current_turn(notify=False)

    async def _next_utterance(self) -> str:
        """Wait for an utterance; meanwhile surface the result of a turn that finishes.

        Waiting on both is what lets a finished turn close the session (``SessionClosing``)
        and guarantees every turn's exception is retrieved. After a stop request no new
        turn starts: utterances that arrive then are not answered.
        """
        while True:
            if self._stop.is_set():
                await self._end_after_turn()
            getter = asyncio.ensure_future(self._inbox.get())
            stop = asyncio.ensure_future(self._stop.wait())
            waiting: set[asyncio.Future[object]] = {getter, stop}  # type: ignore[arg-type]
            turn = self._current_turn
            if turn is not None:
                waiting.add(turn)  # type: ignore[arg-type]
            try:
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (getter, stop):
                    if not waiter.done():
                        waiter.cancel()
            if turn is not None and turn.done():
                self._current_turn = None
                _reraise(turn)
            if stop.done():
                # The stop wins over an utterance that arrived at the same moment: it
                # would cancel the running turn.
                if getter.done() and not getter.cancelled():
                    log.info("utterance_dropped_on_stop")
                continue
            if getter.done() and not getter.cancelled():
                return getter.result()

    async def _end_after_turn(self) -> None:
        """Let the running turn finish until the stop deadline, then end the session.

        A turn still running at the deadline is cancelled like an interrupted one: the
        client gets ``turn_cancelled`` and the partial reply is saved as ``interrupted``.
        Always raises ``SessionClosing``; a turn that ended the call ends it as usual.
        """
        turn = self._current_turn
        if turn is not None:
            remaining = self._stop_deadline - asyncio.get_running_loop().time()
            await asyncio.wait({turn}, timeout=max(remaining, 0))
            await self._cancel_current_turn()
        await self.emit(protocol.SessionEnd(reason=self._stop_reason))
        if self._stop_reason == "shutdown":
            raise SessionClosing(code=SERVICE_RESTART)
        raise SessionClosing(keep_state=False)

    async def _cancel_current_turn(self, *, notify: bool = True) -> None:
        """Cancel the running turn, wait until it has cleaned up, tell the client."""
        turn, self._current_turn = self._current_turn, None
        if turn is None:
            return
        if turn.done():
            _reraise(turn)
            return
        turn_id = self.state.turn
        turn.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            # `suppress(CancelledError)` would also swallow a cancellation of turn_loop
            # itself arriving right now; only the turn's own cancellation is expected.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        if notify:
            await self.emit(protocol.TurnCancelled(turn_id=turn_id))

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
        if self._resumed:
            await self.emit(
                protocol.SessionStarted(
                    call_id=self.call_id,
                    bot_version=self.state.bot_version,
                    resumed=True,
                    history=self.state.transcript(),
                )
            )
            if self.state.current_node is not None:
                await self.emit(protocol.NodeEnter(node_id=self.state.current_node))
            self._check_turn_limit()
            return
        await self.emit(
            protocol.SessionStarted(call_id=self.call_id, bot_version=self.state.bot_version)
        )
        timer = TurnTimer()
        outcome = await self._executor.start(self.state, timer)
        await self._save()
        await self._end_turn(outcome, timer)

    async def _run_turn(self, text: str) -> None:
        """One subscriber turn, saved before its ``turn_end``."""
        if self._recorder is not None:
            self._recorder.user_message(text)
        timer = TurnTimer()
        outcome = await self._handle(text, timer)
        await self._save()
        if outcome is not None:
            await self._end_turn(outcome, timer)
        self._check_turn_limit()

    async def _handle(self, text: str, timer: TurnTimer) -> TurnOutcome | None:
        """Run the turn; a failure becomes an ``error`` event and ``None``, the call goes on."""
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
            return None
        except ProviderError as err:
            log.warning("llm_unavailable", error=str(err), retryable=err.retryable)
            await self.emit(
                protocol.Error(
                    code="llm_unavailable",
                    message="The language model is not available.",
                    retryable=err.retryable,
                )
            )
            return None
        except Exception:
            log.exception("turn_failed", turn_id=self.state.turn)
            await self.emit(protocol.Error(code="internal", message="The turn failed."))
            return None
        return outcome

    async def _end_turn(self, outcome: TurnOutcome, timer: TurnTimer) -> None:
        await self.emit(
            protocol.TurnEnd(
                turn_id=self.state.turn, timings=timer.to_timings(), sources=outcome.sources
            )
        )
        if outcome.ended is not None:
            await self.emit(protocol.SessionEnd(reason=outcome.ended))
            raise SessionClosing(keep_state=False)

    # --- limits --------------------------------------------------------------------

    def _check_turn_limit(self) -> None:
        """Stop after the last allowed subscriber turn (turn 1 is the greeting)."""
        if self._limits is not None and self.state.turn - 1 >= self._limits.max_turns:
            self._request_stop("limit", asyncio.get_running_loop().time())

    def _schedule_time_limit(self) -> asyncio.TimerHandle | None:
        """Stop when the call's time is up, counted from its start, across reconnects.

        The running turn may still finish: it has ``turn_timeout`` past the limit.
        """
        if self._limits is None:
            return None
        elapsed = (datetime.now(UTC) - self.state.started_at).total_seconds()
        left = max(self._limits.max_seconds - elapsed, 0.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + left + self._turn_timeout
        return loop.call_later(left, self._request_stop, "limit", deadline)

    # --- state ---------------------------------------------------------------------

    async def _save(self) -> None:
        """Persist the state; a Redis failure costs only resumability, not the call."""
        if self._store is None:
            return
        try:
            saved = await self._store.save(self.state)
        except RedisError as err:
            log.warning("session_save_failed", error=str(err))
            return
        if not saved:
            log.info("session_superseded")
            raise SessionClosing(code=SUPERSEDED)

    async def _persist_on_close(self, closing: SessionClosing) -> None:
        """Save or drop the state before the socket closes, so a fast reconnect sees it."""
        if self._store is None:
            return
        try:
            async with asyncio.timeout(FLUSH_TIMEOUT):
                if closing.keep_state:
                    await self._store.save(self.state)
                else:
                    await self._store.drop()
        except (RedisError, TimeoutError) as err:
            log.warning("session_save_failed", error=str(err))

    # --- closing -------------------------------------------------------------------

    async def _finish(self, closing: SessionClosing) -> None:
        await self._persist_on_close(closing)
        if closing.flush:
            for event in self._outbox.take_all():
                await self._send_quietly(event)
        if closing.code is not None:
            with suppress(Exception):
                await self._transport.close(code=closing.code)
        if self._recorder is not None:
            self._recorder.closed(closing.code, resumable=closing.keep_state)
        log.info("session_closed", code=closing.code)

    async def _send_quietly(self, event: protocol.ProtocolMessage) -> None:
        """Best-effort send while closing: a gone or stuck client must not block it."""
        with suppress(Exception):
            async with asyncio.timeout(FLUSH_TIMEOUT):
                await self._transport.send_text(event.model_dump_json())


def _reraise(turn: asyncio.Task[None]) -> None:
    """Retrieve a finished turn's outcome; ``run_turn`` lets only ``SessionClosing`` out."""
    if not turn.cancelled() and (exc := turn.exception()) is not None:
        raise exc


def _invalid_message(err: ValidationError) -> protocol.Error:
    first = err.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "frame"
    message = f"Invalid message: {where}: {first['msg']}"
    return protocol.Error(code="invalid_message", message=message)
