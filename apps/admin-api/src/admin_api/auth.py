"""Admin login and logout, session cookie, CSRF and role checks (FR-14).

Routes depend on ``CurrentSession`` to require a logged-in user and on ``require_admin``
to forbid viewers. Both check CSRF for mutating methods: the SPA sends the token it got from
``POST /auth/login`` or ``GET /auth/me`` in the ``X-CSRF-Token`` header. ``SameSite=Lax``
already keeps the cookie off cross-site POSTs; the token covers what Lax does not (same-site
subdomains, older browsers).
"""

import asyncio
import hmac
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.errors import ApiError
from admin_api.passwords import dummy_hash, verify_password
from admin_api.sessions import COOKIE_NAME, CSRF_HEADER, AdminSession, Role, SessionStore
from rozmova_core.models import User
from rozmova_core.settings import Settings

log = structlog.get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.sessions
    return store


async def current_session(request: Request) -> AdminSession:
    session_id = request.cookies.get(COOKIE_NAME)
    session = await _store(request).get(session_id) if session_id else None
    if session is None:
        raise ApiError(401, "not_authenticated", "Log in first.")
    if request.method not in SAFE_METHODS:
        token = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(token.encode(), session.csrf_token.encode()):
            raise ApiError(403, "csrf_failed", f"Missing or wrong {CSRF_HEADER} header.")
    return session


CurrentSession = Annotated[AdminSession, Depends(current_session)]


async def require_admin(session: CurrentSession) -> AdminSession:
    if session.role != "admin":
        raise ApiError(403, "forbidden", "This action needs the admin role.")
    return session


AdminOnly = Annotated[AdminSession, Depends(require_admin)]


class LoginRequest(BaseModel):
    email: str
    password: SecretStr


class UserOut(BaseModel):
    id: str
    email: str
    role: Role


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


def _session_out(session: AdminSession) -> SessionOut:
    return SessionOut(
        user=UserOut(id=session.user_id, email=session.email, role=session.role),
        csrf_token=session.csrf_token,
    )


def _secure_cookie(settings: Settings) -> bool:
    # Local http://localhost has no TLS; every deployed environment does.
    return settings.env not in ("local", "test")


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> SessionOut:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    settings: Settings = request.app.state.settings
    email = normalize_email(body.email)
    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
    password = body.password.get_secret_value()
    # An unknown email costs one hash check too, so timing does not tell which emails exist.
    stored = user.password_hash if user is not None else dummy_hash()
    valid = await asyncio.to_thread(verify_password, password, stored)
    if user is None or not valid:
        log.info("login_failed")
        raise ApiError(401, "invalid_credentials", "Wrong email or password.")

    store = _store(request)
    if old := request.cookies.get(COOKIE_NAME):
        await store.delete(old)
    role: Role = "admin" if user.role == "admin" else "viewer"
    session_id, session = await store.create(str(user.id), user.email, role)
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(settings),
        path="/",
    )
    log.info("login", user_id=session.user_id, role=role)
    return _session_out(session)


@router.post("/logout", status_code=204)
async def logout(session: CurrentSession, request: Request, response: Response) -> None:
    session_id = request.cookies[COOKIE_NAME]  # present: current_session found the session
    await _store(request).delete(session_id)
    settings: Settings = request.app.state.settings
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=_secure_cookie(settings), path="/"
    )
    log.info("logout", user_id=session.user_id)


@router.get("/me")
async def me(session: CurrentSession) -> SessionOut:
    return _session_out(session)
