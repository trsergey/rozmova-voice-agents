"""A bot's LLM provider keys (FR-17): write-only, shown as ``last4``.

``PUT /bots/{id}/credentials`` encrypts the key with the master key
(``rozmova_core.credentials``) and stores one key per bot and provider; a second PUT
replaces it. No response, log line or error ever carries the key: the request model holds
it as ``SecretStr`` and validation errors drop the input (``errors``). After the commit
``bot:published`` tells gateways to drop the bot's cached keys; if Redis fails, they pick
the new key up within their cache TTL.
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from admin_api.auth import AdminOnly, current_session
from admin_api.bots import get_bot
from admin_api.errors import ApiError
from rozmova_core import live_bots
from rozmova_core.credentials import CredentialCipher, last4
from rozmova_core.models import ProviderCredential
from rozmova_engine.providers import registry as providers

log = structlog.get_logger(__name__)


class CredentialIn(BaseModel):
    provider: str
    api_key: Annotated[SecretStr, Field(min_length=8, max_length=500)]

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, provider: str) -> str:
        if provider not in providers.names:
            raise ValueError(f"unknown provider; one of {sorted(providers.names)}")
        return provider


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    last4: str
    created_at: datetime


def _db(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db
    return factory


def _cipher(request: Request) -> CredentialCipher:
    cipher: CredentialCipher | None = request.app.state.cipher
    if cipher is None:
        raise ApiError(
            503, "credentials_unavailable", "No master key to encrypt provider keys with."
        )
    return cipher


router = APIRouter(prefix="/bots", tags=["credentials"], dependencies=[Depends(current_session)])


@router.get("/{bot_id}/credentials")
async def list_credentials(bot_id: uuid.UUID, request: Request) -> list[CredentialOut]:
    async with _db(request)() as db:
        await get_bot(db, bot_id)
        rows = await db.scalars(
            select(ProviderCredential)
            .where(ProviderCredential.bot_id == bot_id)
            .order_by(ProviderCredential.provider)
        )
        return [CredentialOut.model_validate(row) for row in rows]


@router.put("/{bot_id}/credentials")
async def put_credential(
    bot_id: uuid.UUID, body: CredentialIn, request: Request, admin: AdminOnly
) -> CredentialOut:
    ciphertext = _cipher(request).encrypt(body.api_key)
    async with _db(request)() as db, db.begin():
        await get_bot(db, bot_id)
        values = {"secret_ciphertext": ciphertext, "last4": last4(body.api_key)}
        result = await db.execute(
            insert(ProviderCredential)
            .values(bot_id=bot_id, provider=body.provider, **values)
            .on_conflict_do_update(
                constraint="uq_provider_credentials_bot_id_provider",
                set_={**values, "created_at": func.now()},
            )
            .returning(ProviderCredential)
        )
        out = CredentialOut.model_validate(result.scalar_one())
    log.info("credential_saved", bot_id=str(bot_id), provider=body.provider, user_id=admin.user_id)
    redis: Redis = request.app.state.redis
    try:
        await live_bots.signal_changed(redis, bot_id)
    except (RedisError, OSError):
        log.exception("credential_signal_failed", bot_id=str(bot_id))
    return out
