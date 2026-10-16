"""Call events to the Redis Stream ``call-events`` (FR-13, ADR 0005); admin-api persists them.

Two parts:

- ``CallEventWriter``: one per process. ``record`` only puts the event in a bounded queue
  and never waits, so a slow or down Redis cannot hold up a reply; a background task sends
  the queue in pipelined batches of ``XADD … MAXLEN ~ 100000``. A full queue or a failed
  batch drops events with a warning: the call log is best effort, the call is not.
- ``CallRecorder``: one per call. The session hands it every protocol event before the
  debug filter, so tool calls and ``node_enter`` are recorded for viewers' calls too. The
  streamed deltas are joined into one ``assistant_message`` per turn; ``user_message`` and
  ``session_closed`` are recorded by the session itself.

The entry format is ``rozmova_core.call_events.CallEvent``, shared with admin-api.
"""

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import trace
from redis.asyncio import Redis
from redis.exceptions import RedisError

from rozmova_contracts import protocol
from rozmova_core.call_events import MAXLEN, STREAM, CallEvent

log = structlog.get_logger(__name__)

QUEUE_SIZE = 1_000
BATCH_SIZE = 100
XADD_TIMEOUT = 2.0
# Log a dropped event the first time and then every this many, not on every event.
DROP_LOG_EVERY = 100


class CallEventWriter:
    def __init__(self, redis: Redis, *, queue_size: int = QUEUE_SIZE) -> None:
        self._redis = redis
        self._queue: asyncio.Queue[CallEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="call-events-writer")

    def record(self, event: CallEvent) -> None:
        """Queue ``event``; never waits. A full queue drops it."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop(1, "queue_full")

    async def aclose(self, *, flush_seconds: float = XADD_TIMEOUT) -> None:
        """Send what is queued, for at most ``flush_seconds``, then stop."""
        if self._task is None:
            return
        try:
            async with asyncio.timeout(flush_seconds):
                await self._queue.join()
        except TimeoutError:
            log.warning("call_events_not_flushed", left=self._queue.qsize())
        self._task.cancel()
        await asyncio.wait([self._task])
        self._task = None

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < BATCH_SIZE and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._send(batch)
            except (RedisError, TimeoutError) as err:
                self._drop(len(batch), "redis", error=str(err))
            except Exception as err:  # the writer must outlive any one bad batch
                log.exception("call_events_batch_failed")
                self._drop(len(batch), "error", error=type(err).__name__)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _send(self, batch: list[CallEvent]) -> None:
        async with asyncio.timeout(XADD_TIMEOUT), self._redis.pipeline(transaction=False) as pipe:
            for event in batch:
                pipe.xadd(STREAM, event.fields(), maxlen=MAXLEN, approximate=True)  # type: ignore[arg-type]
            await pipe.execute()

    def _drop(self, count: int, reason: str, **details: str) -> None:
        before = self.dropped
        self.dropped += count
        if before == 0 or before // DROP_LOG_EVERY != self.dropped // DROP_LOG_EVERY:
            log.warning("call_events_dropped", reason=reason, dropped_total=self.dropped, **details)


class CallRecorder:
    """The events of one call, for ``CallEventWriter``."""

    def __init__(
        self,
        writer: CallEventWriter,
        *,
        call_id: UUID,
        bot_id: UUID,
        user_id: str,
        debug: bool,
    ) -> None:
        self._writer = writer
        self._call_id = call_id
        self._bot_id = bot_id
        self._user_id = user_id
        self._debug = debug
        # The reply streamed so far, per turn; recorded whole when the turn ends.
        self._replies: dict[int, list[str]] = {}

    def observe(self, event: protocol.ProtocolMessage) -> None:
        """Record a protocol event the session emits (debug-only ones included)."""
        match event:
            case protocol.AssistantDelta(turn_id=turn_id, text=text):
                self._replies.setdefault(turn_id, []).append(text)
                return
            case protocol.TurnEnd(turn_id=turn_id):
                self._assistant_message(turn_id, interrupted=False)
            case protocol.TurnCancelled(turn_id=turn_id):
                self._assistant_message(turn_id, interrupted=True)
        payload = event.model_dump(mode="json")
        type_ = str(payload.pop("type"))
        if isinstance(event, protocol.SessionStarted):
            # Who called and how; the resumed history is already in the earlier events.
            payload.pop("history", None)
            payload |= {"user_id": self._user_id, "debug": self._debug}
        self.record(type_, payload)

    def user_message(self, text: str) -> None:
        self.record("user_message", {"text": text})

    def closed(self, code: int | None, *, resumable: bool) -> None:
        """The connection closed; ``resumable`` when the call may go on after a reconnect."""
        self.record("session_closed", {"code": code, "resumable": resumable})

    def record(self, type_: str, payload: Mapping[str, Any]) -> None:
        span = trace.get_current_span().get_span_context()
        self._writer.record(
            CallEvent(
                call_id=self._call_id,
                bot_id=self._bot_id,
                type=type_,
                payload=payload,
                trace_id=format(span.trace_id, "032x") if span.is_valid else None,
                span_id=format(span.span_id, "016x") if span.is_valid else None,
            )
        )

    def _assistant_message(self, turn_id: int, *, interrupted: bool) -> None:
        parts = self._replies.pop(turn_id, None)
        if parts:
            self.record(
                "assistant_message",
                {"turn_id": turn_id, "text": "".join(parts), "interrupted": interrupted},
            )
