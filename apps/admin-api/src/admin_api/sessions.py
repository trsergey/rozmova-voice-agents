"""Admin web sessions kept in Redis (ADR 0014).

The browser holds only an opaque random id in an httpOnly cookie. Redis keeps
``admin-session:{sha256(id)}`` with the user, role and CSRF token: logout deletes the key
and the session is gone at once, and a leaked Redis dump holds no usable cookie values.
Every read extends the TTL (``GETEX``), so a session expires after a period of inactivity.
"""

import hashlib
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

Role = Literal["admin", "viewer"]

COOKIE_NAME = "rozmova_session"
CSRF_HEADER = "X-CSRF-Token"


class AdminSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    email: str
    role: Role
    csrf_token: str


def _key(session_id: str) -> str:
    return f"admin-session:{hashlib.sha256(session_id.encode()).hexdigest()}"


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def create(self, user_id: str, email: str, role: Role) -> tuple[str, AdminSession]:
        """Start a session; returns the cookie value and the session."""
        session_id = secrets.token_urlsafe(32)
        session = AdminSession(
            user_id=user_id, email=email, role=role, csrf_token=secrets.token_urlsafe(32)
        )
        await self._redis.set(_key(session_id), session.model_dump_json(), ex=self._ttl)
        return session_id, session

    async def get(self, session_id: str) -> AdminSession | None:
        """The live session for a cookie value, or ``None``; extends its TTL."""
        raw = await self._redis.getex(_key(session_id), ex=self._ttl)
        if raw is None:
            return None
        try:
            return AdminSession.model_validate_json(raw)
        except ValidationError:
            return None

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(_key(session_id))
