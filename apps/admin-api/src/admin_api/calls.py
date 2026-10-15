"""``POST /calls/ticket``: a short-lived signed ticket to open a call on the gateway
(FR-14, ADR 0010). Signing and the single-use key live in ``rozmova_core.tickets``, shared
with the gateway that verifies them.
"""

from urllib.parse import urlencode
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from admin_api.auth import CurrentSession
from admin_api.errors import ApiError
from admin_api.sessions import AdminSession
from rozmova_core import tickets
from rozmova_core.settings import Settings

log = structlog.get_logger(__name__)


class TicketRequest(BaseModel):
    bot_id: UUID
    # Set to reconnect to a call that is still live (FR-21); a new call otherwise.
    call_id: UUID | None = None
    # Debug events (node_enter, tool calls) for the admin's test console only.
    debug: bool = False


class TicketOut(BaseModel):
    ticket: str
    call_id: UUID
    ws_url: str  # ready to open; the ticket is valid for 60 s and only once


def ticket_user_id(session: AdminSession) -> str:
    if session.user_id is not None:
        return f"user:{session.user_id}"
    return f"code:{session.access_code_id}"


def ws_url(gateway_url: str, bot_id: UUID, call_id: UUID, ticket: str) -> str:
    base = gateway_url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{base}/ws/calls/{bot_id}?{urlencode({'call_id': call_id, 'ticket': ticket})}"


router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/ticket")
async def issue_ticket(body: TicketRequest, session: CurrentSession, request: Request) -> TicketOut:
    settings: Settings = request.app.state.settings
    if body.debug and session.role != "admin":
        raise ApiError(403, "forbidden", "Debug calls are for the admin's test console.")
    if settings.ticket_secret is None:
        log.error("ticket_secret_unset")
        raise ApiError(503, "tickets_unavailable", "Calls are not configured on this server.")

    redis: Redis = request.app.state.redis
    call_id = body.call_id or uuid4()
    token = await tickets.issue(
        redis,
        settings.ticket_secret.get_secret_value(),
        user_id=ticket_user_id(session),
        bot_id=body.bot_id,
        call_id=call_id,
        debug=body.debug,
    )
    log.info(
        "ticket_issued",
        call_id=str(call_id),
        bot_id=str(body.bot_id),
        user_id=ticket_user_id(session),
        debug=body.debug,
        reconnect=body.call_id is not None,
    )
    return TicketOut(
        ticket=token,
        call_id=call_id,
        ws_url=ws_url(settings.gateway_public_url, body.bot_id, call_id, token),
    )
