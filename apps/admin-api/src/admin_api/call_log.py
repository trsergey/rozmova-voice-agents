"""The call log consumer (FR-13, ADR 0005): the Redis Stream ``call-events`` into PostgreSQL.

One background task in admin-api reads the stream with the consumer group ``persist``:

1. On start it creates the group if needed and takes over entries another consumer left
   pending for longer than ``CLAIM_IDLE_MS`` (``XAUTOCLAIM``): a crashed instance.
2. Then it reads its own pending entries (``XREADGROUP`` from ``0``), and new ones (``>``),
   polling every ``POLL_SECONDS`` when there are none. Not a blocking read: it would hold a
   connection of the shared pool and race the client's 5 s socket timeout.
3. A batch is written in one transaction: the ``calls`` row (created on ``session_started``,
   or with what is known when another event comes first; ``ended_at`` and ``outcome`` on
   ``session_end``) and the ``call_events`` rows, inserted ``ON CONFLICT DO NOTHING`` by the
   stream entry id, so a redelivered entry is not stored twice.
4. After the commit the entries are acknowledged (``XACK``) and published to ``calls:live``.

A call whose connection closed without ``session_end`` (the subscriber closed the tab) may
be resumed for 30 minutes (the gateway's session TTL), so its row stays open. Once a minute
the consumer closes calls with no event for ``ABANDONED_AFTER``: ``ended_at`` is their last
event and ``outcome`` is ``abandoned``. A live call cannot be that quiet: the session limit
ends it after 10 minutes.

The write of a batch is the span ``call_log.persist``, linked to the span of every event
that came with a trace context, so a call's trace leads to where its events were stored.

A batch that fails to commit is not acknowledged; the consumer backs off and reads its
pending entries again. A malformed entry, or one of a bot that no longer exists, is logged
and acknowledged: it would fail forever otherwise.
"""

import asyncio
import json
import os
import socket
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from opentelemetry import trace
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rozmova_core.call_events import STREAM, CallEvent
from rozmova_core.models import AccessCode, Bot, BotVersion, Call, User
from rozmova_core.models import CallEvent as CallEventRow

log = structlog.get_logger(__name__)
tracer = trace.get_tracer("rozmova.admin_api")

GROUP = "persist"
LIVE_CHANNEL = "calls:live"
BATCH = 100
POLL_SECONDS = 0.5
# A pending entry idle this long belongs to a consumer that is gone.
CLAIM_IDLE_MS = 60_000
RETRY_SECONDS = (1.0, 2.0, 5.0, 10.0)
# The gateway keeps a closed call resumable for 30 minutes; a margin on top of that.
ABANDONED_AFTER = timedelta(minutes=35)
ABANDONED_OUTCOME = "abandoned"
SWEEP_SECONDS = 60.0

Entry = tuple[str, dict[str, str]]


def consumer_name() -> str:
    """Unique per process: two instances during a deploy overlap must not share pending."""
    return f"admin-api-{socket.gethostname()}-{os.getpid()}"


def _subject(user_id: str) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """``user:<uuid>`` or ``code:<uuid>`` from the ticket → (user id, access code id)."""
    kind, _, raw = user_id.partition(":")
    try:
        value = uuid.UUID(raw)
    except ValueError:
        return None, None
    if kind == "user":
        return value, None
    if kind == "code":
        return None, value
    return None, None


class CallLogConsumer:
    def __init__(
        self,
        redis: Redis,
        db: async_sessionmaker[AsyncSession],
        *,
        name: str | None = None,
        poll_seconds: float = POLL_SECONDS,
        claim_idle_ms: int = CLAIM_IDLE_MS,
    ) -> None:
        self._redis = redis
        self._db = db
        self.name = name or consumer_name()
        self._poll_seconds = poll_seconds
        self._claim_idle_ms = claim_idle_ms

    async def run(self) -> None:
        """Consume until cancelled; Redis or database failures are retried with backoff."""
        failures = 0
        claimed = False
        own_pending = True
        next_sweep = 0.0
        while True:
            try:
                if not claimed:
                    await self.ensure_group()
                    await self.claim_stale()
                    claimed = True
                if time.monotonic() >= next_sweep:
                    await self.close_abandoned()
                    next_sweep = time.monotonic() + SWEEP_SECONDS
                entries = await self.read(pending=own_pending)
                if entries:
                    await self.persist(entries)
                elif own_pending:
                    own_pending = False
                else:
                    await asyncio.sleep(self._poll_seconds)
                failures = 0
            except Exception:  # anything but cancellation: back off and try again
                log.exception("call_log_failed", consumer=self.name)
                own_pending = True
                delay = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
                failures += 1
                await asyncio.sleep(delay)

    async def close_abandoned(self, now: datetime | None = None) -> int:
        """Close open calls whose last event is older than ``ABANDONED_AFTER``."""
        cutoff = (now or datetime.now(UTC)) - ABANDONED_AFTER
        last = (
            select(CallEventRow.call_id, func.max(CallEventRow.ts).label("ts"))
            .join(Call, Call.id == CallEventRow.call_id)
            .where(Call.ended_at.is_(None))
            .group_by(CallEventRow.call_id)
            .subquery()
        )
        async with self._db() as db, db.begin():
            result = await db.execute(
                update(Call)
                .where(Call.id == last.c.call_id, Call.ended_at.is_(None), last.c.ts < cutoff)
                .values(ended_at=last.c.ts, outcome=ABANDONED_OUTCOME)
                .returning(Call.id)
            )
            closed = len(result.all())
        if closed:
            log.info("calls_abandoned", count=closed)
        return closed

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as err:
            if "BUSYGROUP" not in str(err):
                raise

    async def claim_stale(self) -> None:
        """Take over and persist entries left pending by a consumer that is gone."""
        start = "0-0"
        while True:
            result: Any = await self._redis.xautoclaim(
                STREAM, GROUP, self.name, self._claim_idle_ms, start_id=start, count=BATCH
            )
            start, entries = result[0], [e for e in result[1] if e[1] is not None]
            if entries:
                log.info("call_log_claimed", count=len(entries))
                await self.persist(entries)
            if start == "0-0":
                return

    async def read(self, *, pending: bool) -> list[Entry]:
        """This consumer's pending entries (``pending``) or new ones; never blocks."""
        result: Any = await self._redis.xreadgroup(
            GROUP, self.name, {STREAM: "0" if pending else ">"}, count=BATCH
        )
        if not result:
            return []
        _, entries = result[0]
        return [(entry_id, fields) for entry_id, fields in entries if fields]

    async def persist(self, entries: Sequence[Entry]) -> None:
        events: list[tuple[str, CallEvent]] = []
        for entry_id, fields in entries:
            try:
                events.append((entry_id, CallEvent.from_fields(fields)))
            except (KeyError, ValueError) as err:
                log.warning("call_event_malformed", entry_id=entry_id, error=str(err))
        stored: list[tuple[str, CallEvent]] = []
        links = [trace.Link(context) for _, event in events if (context := _span_context(event))]
        with tracer.start_as_current_span(
            "call_log.persist", links=links, attributes={"call_log.entries": len(entries)}
        ):
            await self._write(events, stored)
        await self._redis.xack(STREAM, GROUP, *[entry_id for entry_id, _ in entries])
        await self._publish(stored)

    async def _write(
        self, events: list[tuple[str, CallEvent]], stored: list[tuple[str, CallEvent]]
    ) -> None:
        async with self._db() as db, db.begin():
            for entry_id, event in events:
                if await _store(db, entry_id, event):
                    stored.append((entry_id, event))
                else:
                    log.warning(
                        "call_event_skipped",
                        entry_id=entry_id,
                        reason="bot_not_found",
                        bot_id=str(event.bot_id),
                    )

    async def _publish(self, stored: list[tuple[str, CallEvent]]) -> None:
        """Best effort: the live view may miss an event, the call log does not."""
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for entry_id, event in stored:
                    pipe.publish(LIVE_CHANNEL, _live_message(entry_id, event))
                await pipe.execute()
        except RedisError as err:
            log.warning("calls_live_publish_failed", error=str(err))


def _span_context(event: CallEvent) -> trace.SpanContext | None:
    """The span the gateway recorded the event in, if one was recording."""
    if event.trace_id is None or event.span_id is None:
        return None
    try:
        return trace.SpanContext(
            trace_id=int(event.trace_id, 16),
            span_id=int(event.span_id, 16),
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    except ValueError:
        return None


def _live_message(entry_id: str, event: CallEvent) -> str:
    return json.dumps(
        {
            "id": entry_id,
            "call_id": str(event.call_id),
            "bot_id": str(event.bot_id),
            "type": event.type,
            "ts": event.ts.isoformat(),
            "payload": event.payload,
        },
        ensure_ascii=False,
    )


async def _store(db: AsyncSession, entry_id: str, event: CallEvent) -> bool:
    """Write the event and its call; ``False`` when the bot no longer exists."""
    if not await _ensure_call(db, event):
        return False
    await db.execute(
        insert(CallEventRow)
        .values(
            id=entry_id,
            call_id=event.call_id,
            ts=event.ts,
            type=event.type,
            payload=dict(event.payload),
        )
        .on_conflict_do_nothing(index_elements=[CallEventRow.id])
    )
    if event.type == "session_end":
        await db.execute(
            update(Call)
            .where(Call.id == event.call_id)
            .values(ended_at=event.ts, outcome=str(event.payload.get("reason")))
        )
    return True


async def _ensure_call(db: AsyncSession, event: CallEvent) -> bool:
    """Create or complete the ``calls`` row. References that do not exist (a deleted user or
    code, an unknown version) are stored as NULL rather than failing the batch."""
    if not await db.scalar(select(exists().where(Bot.id == event.bot_id))):
        return False
    values: dict[str, Any] = {"id": event.call_id, "bot_id": event.bot_id, "started_at": event.ts}
    if event.type == "session_started":
        user_id, code_id = _subject(str(event.payload.get("user_id", "")))
        version = event.payload.get("bot_version")
        values |= {
            "bot_version_id": select(BotVersion.id)
            .where(BotVersion.bot_id == event.bot_id, BotVersion.version == version)
            .scalar_subquery()
            if isinstance(version, int)
            else None,
            "user_id": select(User.id).where(User.id == user_id).scalar_subquery()
            if user_id
            else None,
            "access_code_id": select(AccessCode.id)
            .where(AccessCode.id == code_id)
            .scalar_subquery()
            if code_id
            else None,
        }
    stmt = insert(Call).values(**values)
    if event.type == "session_started":
        # A resumed call, or a row an earlier event created with what it knew.
        stmt = stmt.on_conflict_do_update(
            index_elements=[Call.id],
            set_={
                "bot_version_id": func.coalesce(Call.bot_version_id, stmt.excluded.bot_version_id),
                "user_id": func.coalesce(Call.user_id, stmt.excluded.user_id),
                "access_code_id": func.coalesce(Call.access_code_id, stmt.excluded.access_code_id),
                "started_at": func.least(Call.started_at, stmt.excluded.started_at),
            },
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Call.id])
    await db.execute(stmt)
    return True


def start_call_log(redis: Redis, db: async_sessionmaker[AsyncSession]) -> asyncio.Task[None]:
    return asyncio.create_task(CallLogConsumer(redis, db).run(), name="call-log")


async def stop_call_log(task: asyncio.Task[None] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        # wait() neither raises the task's CancelledError nor swallows our own.
        await asyncio.wait([task])
