"""The call log for people (FR-13, S5): the list, one call's timeline and the live feed.

``call_log`` fills ``calls`` and ``call_events`` from the gateway's stream; this module reads
them. Admins see every call. A viewer sees only the calls made under their own login or
access code: a code is handed to one company, and other companies' conversations are not
theirs to read. Another's call answers 404, as if it did not exist.

The list is newest first with keyset pagination: ``next_cursor`` is the position after the
last item, so new calls arriving between pages neither repeat nor skip rows.
``WS /calls/live`` forwards ``calls:live`` (published after each persisted batch) to admins.
"""

import asyncio
import base64
import contextlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Query, Request, WebSocket
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, Select, and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import CurrentSession
from admin_api.call_log import LIVE_CHANNEL
from admin_api.errors import ApiError
from admin_api.sessions import COOKIE_NAME, AdminSession, SessionStore
from rozmova_core.models import AccessCode, Bot, BotVersion, Call, User
from rozmova_core.models import CallEvent as CallEventRow

log = structlog.get_logger(__name__)

# ``open``: no ``session_end`` yet, a call in progress or one whose gateway went away.
Outcome = Literal["hangup", "timeout", "shutdown", "limit", "handoff", "completed", "open"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# How often the live feed re-checks that its session still exists (logout, revoked code).
SESSION_CHECK_SECONDS = 30.0


class CallOut(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot_name: str
    bot_version: int | None  # None for a test-console call on the draft
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    caller: str | None  # the user's email or the access code's label
    turns: int  # messages from the caller


class CallPage(BaseModel):
    items: list[CallOut]
    next_cursor: str | None


class CallEventOut(BaseModel):
    id: str
    ts: datetime
    type: str
    payload: dict[str, Any]


class CallDetailOut(CallOut):
    events: list[CallEventOut]


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


def _calls() -> Select[Any]:
    turns = (
        select(func.count())
        .where(CallEventRow.call_id == Call.id, CallEventRow.type == "user_message")
        .correlate(Call)
        .scalar_subquery()
    )
    return (
        select(
            Call,
            Bot.name,
            BotVersion.version,
            func.coalesce(User.email, AccessCode.label),
            turns,
        )
        .join(Bot, Bot.id == Call.bot_id)
        .outerjoin(BotVersion, BotVersion.id == Call.bot_version_id)
        .outerjoin(User, User.id == Call.user_id)
        .outerjoin(AccessCode, AccessCode.id == Call.access_code_id)
    )


def _visible_to(session: AdminSession) -> ColumnElement[bool] | None:
    if session.role == "admin":
        return None
    if session.access_code_id is not None:
        return Call.access_code_id == uuid.UUID(session.access_code_id)
    return Call.user_id == uuid.UUID(str(session.user_id))


def _call_out(row: Any) -> CallOut:
    call, bot_name, version, caller, turns = row
    return CallOut(
        id=call.id,
        bot_id=call.bot_id,
        bot_name=bot_name,
        bot_version=version,
        started_at=call.started_at,
        ended_at=call.ended_at,
        outcome=call.outcome,
        caller=caller,
        turns=turns,
    )


def _encode_cursor(call: CallOut) -> str:
    raw = f"{call.started_at.isoformat()}|{call.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        started_at, call_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(started_at), uuid.UUID(call_id)
    except ValueError as exc:  # also binascii.Error and UnicodeDecodeError
        raise ApiError(422, "bad_cursor", "The cursor is not one this API returned.") from exc


router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("")
async def list_calls(
    request: Request,
    session: CurrentSession,
    bot_id: uuid.UUID | None = None,
    outcome: Outcome | None = None,
    since: Annotated[datetime | None, Query(description="Started at or after")] = None,
    until: Annotated[datetime | None, Query(description="Started before")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> CallPage:
    conditions: list[ColumnElement[bool]] = []
    if (visible := _visible_to(session)) is not None:
        conditions.append(visible)
    if bot_id is not None:
        conditions.append(Call.bot_id == bot_id)
    if outcome == "open":
        conditions.append(Call.ended_at.is_(None))
    elif outcome is not None:
        conditions.append(Call.outcome == outcome)
    if since is not None:
        conditions.append(Call.started_at >= since)
    if until is not None:
        conditions.append(Call.started_at < until)
    if cursor is not None:
        conditions.append(tuple_(Call.started_at, Call.id) < tuple_(*_decode_cursor(cursor)))

    async with _db(request)() as db:
        rows = await db.execute(
            _calls()
            .where(and_(True, *conditions))
            .order_by(Call.started_at.desc(), Call.id.desc())
            .limit(limit + 1)
        )
        items = [_call_out(row) for row in rows]
    more = len(items) > limit
    items = items[:limit]
    return CallPage(items=items, next_cursor=_encode_cursor(items[-1]) if more else None)


@router.get("/{call_id}")
async def read_call(call_id: uuid.UUID, request: Request, session: CurrentSession) -> CallDetailOut:
    """The call and its events in order: messages, nodes, tools, timings, errors."""
    stmt = _calls().where(Call.id == call_id)
    if (visible := _visible_to(session)) is not None:
        stmt = stmt.where(visible)
    async with _db(request)() as db:
        row = (await db.execute(stmt)).first()
        if row is None:
            raise ApiError(404, "call_not_found", "No such call.")
        events = await db.scalars(
            select(CallEventRow)
            .where(CallEventRow.call_id == call_id)
            .order_by(CallEventRow.ts, CallEventRow.id)
        )
        timeline = [CallEventOut(id=e.id, ts=e.ts, type=e.type, payload=e.payload) for e in events]
    return CallDetailOut(**_call_out(row).model_dump(), events=timeline)


# --- the live feed ------------------------------------------------------------------------


def _same_origin(websocket: WebSocket) -> bool:
    """The SPA is served by this app, so its WebSocket comes from the same origin; this
    keeps another site from opening the feed with the admin's cookie."""
    origin = websocket.headers.get("origin")
    return origin is not None and urlsplit(origin).netloc == websocket.headers.get("host")


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    # accept() first: a close before it becomes HTTP 403 and the browser sees only 1006.
    await websocket.accept()
    await websocket.close(code, reason)


@router.websocket("/live")
async def live(websocket: WebSocket, bot_id: uuid.UUID | None = None) -> None:
    """Every persisted call event as JSON ``{id, call_id, bot_id, type, ts, payload}``,
    optionally for one bot. Admins only: 4401 without a session, 4403 for viewers."""
    store: SessionStore = websocket.app.state.sessions
    session_id = websocket.cookies.get(COOKIE_NAME)
    session = await store.get(session_id) if session_id else None
    if session_id is None or session is None or not _same_origin(websocket):
        await _close(websocket, 4401, "not authenticated")
        return
    if session.role != "admin":
        await _close(websocket, 4403, "admins only")
        return
    await websocket.accept()
    redis: Redis = websocket.app.state.redis
    forward = asyncio.create_task(_forward(websocket, redis, store, session_id, bot_id))
    receive = asyncio.create_task(_until_disconnect(websocket))
    try:
        await asyncio.wait({forward, receive}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (forward, receive):
            task.cancel()
        await asyncio.wait({forward, receive})
    if not forward.cancelled() and (error := forward.exception()) is not None:
        log.warning("calls_live_failed", error=str(error))
        with contextlib.suppress(RuntimeError):  # the client may be gone already
            await websocket.close(1011, "live feed unavailable")


async def _until_disconnect(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _forward(
    websocket: WebSocket,
    redis: Redis,
    store: SessionStore,
    session_id: str,
    bot_id: uuid.UUID | None,
) -> None:
    loop = asyncio.get_running_loop()
    checked_at = loop.time()
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(LIVE_CHANNEL)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None:
                data = str(message["data"])
                if bot_id is None or _bot_of(data) == str(bot_id):
                    await websocket.send_text(data)
            if loop.time() - checked_at > SESSION_CHECK_SECONDS:
                checked_at = loop.time()
                if await store.get(session_id) is None:
                    await websocket.close(4401, "session ended")
                    return


def _bot_of(data: str) -> str | None:
    try:
        bot = json.loads(data).get("bot_id")
    except (ValueError, AttributeError):
        return None
    return bot if isinstance(bot, str) else None
