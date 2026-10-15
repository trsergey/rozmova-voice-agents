"""Signed one-time tickets for the gateway WebSocket (FR-14, ADR 0010).

The admin API issues a ticket (``issue``) and the gateway checks it (``verify`` and
``consume``) before the call starts. The token is
``base64url(payload JSON) "." base64url(HMAC-SHA256(payload))``: a JWT would add a library
and an algorithm header for nothing, both sides are ours and share one secret.

The signature and ``exp`` prove the admin API issued the ticket in the last 60 s; single
use is enforced by Redis: ``ticket:{id}`` is written on issue and deleted by the first
connection that presents it. ``DEL`` is atomic, so two connections racing with the same
ticket cannot both win.
"""

import base64
import binascii
import hashlib
import hmac
import time
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis

TICKET_TTL_SECONDS = 60


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    user_id: str
    bot_id: UUID
    call_id: UUID
    debug: bool = False
    exp: int  # Unix seconds


class TicketError(Exception):
    """The token is malformed, forged or expired. The message is for logs, not clients."""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _mac(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def sign(ticket: Ticket, secret: str) -> str:
    payload = _b64encode(ticket.model_dump_json().encode())
    return f"{payload}.{_mac(payload, secret)}"


def verify(token: str, secret: str, *, now: float | None = None) -> Ticket:
    """Check the signature and expiry and return the ticket. Single use is not checked here."""
    payload, sep, mac = token.partition(".")
    if not sep or not hmac.compare_digest(mac, _mac(payload, secret)):
        raise TicketError("bad signature")
    try:
        ticket = Ticket.model_validate_json(_b64decode(payload))
    except (binascii.Error, ValueError, ValidationError) as exc:
        raise TicketError("bad payload") from exc
    if ticket.exp <= (time.time() if now is None else now):
        raise TicketError("expired")
    return ticket


def _key(ticket_id: UUID) -> str:
    return f"ticket:{ticket_id}"


async def consume(redis: Redis, ticket_id: UUID) -> bool:
    """``True`` exactly once per issued ticket; ``False`` if unknown, used or expired."""
    return bool(await redis.delete(_key(ticket_id)))


async def issue(
    redis: Redis,
    secret: str,
    *,
    user_id: str,
    bot_id: UUID,
    call_id: UUID,
    debug: bool = False,
) -> str:
    """Create, remember and sign a ticket; the admin API's ``POST /calls/ticket`` (ROZ-61)."""
    ticket = Ticket(
        user_id=user_id,
        bot_id=bot_id,
        call_id=call_id,
        debug=debug,
        exp=int(time.time()) + TICKET_TTL_SECONDS,
    )
    await redis.set(_key(ticket.id), "1", ex=TICKET_TTL_SECONDS)
    return sign(ticket, secret)
