"""Demo access codes: the admin issues one per recipient and can revoke it (FR-14, ADR 0010).

A code is 12 characters from an alphabet without look-alikes (no 0/O, 1/I/L), shown as
``XXXX-XXXX-XXXX``: about 59 bits, far beyond guessing over HTTP. Only its SHA-256 is
stored. A password needs a slow salted hash because people pick weak ones; a random code
does not, and an unsalted digest lets login find the code with one indexed lookup.
The plain code is returned once, by ``POST /access-codes``.
"""

import hashlib
import secrets
import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import SessionOut, require_admin, session_out, start_session
from admin_api.errors import ApiError
from admin_api.sessions import SessionStore
from rozmova_core.models import AccessCode

log = structlog.get_logger(__name__)

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12
GROUP = 4


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + GROUP] for i in range(0, CODE_LENGTH, GROUP))


def normalize_code(code: str) -> str:
    """Case, spaces and dashes do not matter when a person types the code."""
    return "".join(ch for ch in code.upper() if ch not in " -\t")


def code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


class AccessCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class IssuedAccessCode(AccessCodeOut):
    code: str  # shown once; only its hash is stored


class AccessCodeLoginRequest(BaseModel):
    code: SecretStr


class IssueRequest(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


login_router = APIRouter(prefix="/auth", tags=["auth"])


@login_router.post("/access-code")
async def access_code_login(
    body: AccessCodeLoginRequest, request: Request, response: Response
) -> SessionOut:
    """Demo sign-in: a valid, unrevoked code gives a viewer session bound to the code."""
    async with _db(request)() as db, db.begin():
        code = await db.scalar(
            update(AccessCode)
            .where(
                AccessCode.code_hash == code_hash(body.code.get_secret_value()),
                AccessCode.revoked_at.is_(None),
            )
            .values(last_used_at=func.now())
            .returning(AccessCode)
        )
    if code is None:
        log.info("access_code_login_failed")
        raise ApiError(401, "invalid_access_code", "This access code is not valid.")

    session = await start_session(
        request, response, name=code.label, role="viewer", access_code_id=str(code.id)
    )
    log.info("access_code_login", access_code_id=session.access_code_id)
    return session_out(session)


router = APIRouter(
    prefix="/access-codes", tags=["access-codes"], dependencies=[Depends(require_admin)]
)


@router.get("")
async def list_codes(request: Request) -> list[AccessCodeOut]:
    async with _db(request)() as db:
        codes = await db.scalars(select(AccessCode).order_by(AccessCode.created_at.desc()))
        return [AccessCodeOut.model_validate(code) for code in codes]


@router.post("", status_code=201)
async def issue_code(body: IssueRequest, request: Request) -> IssuedAccessCode:
    code = generate_code()
    async with _db(request)() as db, db.begin():
        row = AccessCode(label=body.label, code_hash=code_hash(code))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    log.info("access_code_issued", access_code_id=str(row.id))
    return IssuedAccessCode(**AccessCodeOut.model_validate(row).model_dump(), code=code)


@router.delete("/{code_id}", status_code=204)
async def revoke_code(code_id: uuid.UUID, request: Request) -> None:
    async with _db(request)() as db, db.begin():
        row = await db.get(AccessCode, code_id, with_for_update=True)
        if row is None:
            raise ApiError(404, "not_found", "No such access code.")
        if row.revoked_at is None:
            row.revoked_at = await db.scalar(select(func.now()))
    # After the commit, so a new login already sees the code as revoked.
    store: SessionStore = request.app.state.sessions
    ended = await store.end_access_code_sessions(str(code_id))
    log.info("access_code_revoked", access_code_id=str(code_id), sessions_ended=ended)
