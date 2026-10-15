"""Admin web sessions kept in Redis (ADR 0014).

The browser holds only an opaque random id in an httpOnly cookie. Redis keeps
``admin-session:{sha256(id)}`` with the user, role and CSRF token: logout deletes the key
and the session is gone at once, and a leaked Redis dump holds no usable cookie values.
Every read extends the TTL (``GETEX``), so a session expires after a period of inactivity.

A viewer who signed in with an access code is also listed in
``access-code-sessions:{code_id}``, so revoking the code ends those sessions at once.
"""

import hashlib
import secrets
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from redis.asyncio import Redis

Role = Literal["admin", "viewer"]

COOKIE_NAME = "rozmova_session"
CSRF_HEADER = "X-CSRF-Token"


class AdminSession(BaseModel):
    """Exactly one of ``user_id`` (a ``users`` row) and ``access_code_id`` is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str | None = None
    access_code_id: str | None = None
    name: str  # the user's email, or the access code's label
    role: Role
    csrf_token: str

    @model_validator(mode="after")
    def _one_subject(self) -> Self:
        if (self.user_id is None) == (self.access_code_id is None):
            raise ValueError("set exactly one of user_id and access_code_id")
        return self

    @property
    def subject_id(self) -> str:
        return self.user_id or self.access_code_id or ""


def _key(session_id: str) -> str:
    return f"admin-session:{hashlib.sha256(session_id.encode()).hexdigest()}"


def _code_index(access_code_id: str) -> str:
    return f"access-code-sessions:{access_code_id}"


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def create(
        self,
        *,
        name: str,
        role: Role,
        user_id: str | None = None,
        access_code_id: str | None = None,
    ) -> tuple[str, AdminSession]:
        """Start a session; returns the cookie value and the session."""
        session_id = secrets.token_urlsafe(32)
        session = AdminSession(
            user_id=user_id,
            access_code_id=access_code_id,
            name=name,
            role=role,
            csrf_token=secrets.token_urlsafe(32),
        )
        key = _key(session_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(key, session.model_dump_json(), ex=self._ttl)
            if access_code_id is not None:
                pipe.sadd(_code_index(access_code_id), key)
                pipe.expire(_code_index(access_code_id), self._ttl)
            await pipe.execute()
        return session_id, session

    async def get(self, session_id: str) -> AdminSession | None:
        """The live session for a cookie value, or ``None``; extends its TTL."""
        raw = await self._redis.getex(_key(session_id), ex=self._ttl)
        if raw is None:
            return None
        try:
            session = AdminSession.model_validate_json(raw)
        except ValidationError:
            return None
        if session.access_code_id is not None:
            # The index must outlive every session in it, and sessions slide.
            await self._redis.expire(_code_index(session.access_code_id), self._ttl)
        return session

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(_key(session_id))

    async def end_access_code_sessions(self, access_code_id: str) -> int:
        """Delete every session started with this code; returns how many were live.

        A login that read the code as valid just before it was revoked can still create a
        session after this runs; it then lives until its TTL. The window is one request.
        """
        index = _code_index(access_code_id)
        keys = await self._redis.smembers(index)
        if not keys:
            return 0
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(*keys)
            pipe.delete(index)
            deleted, _ = await pipe.execute()
        return int(deleted)
