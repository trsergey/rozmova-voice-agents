"""Where a session gets its bot: flow, LLM provider and model (FR-8, ADR 0012).

``PublishedBotLoader`` serves what admin-api publishes. A new call gets the live version:
from a small in-process cache, else Redis ``bot:{id}:live``, else PostgreSQL
(``bots.live_version_id``), which stays the source of truth. ``bot:published`` drops the
cached entry at once (``watch``); entries also expire after ``ttl`` seconds, so a lost
signal still reaches new calls well within a minute. A reconnecting call loads the exact
version it started on (versions are immutable, so they are cached for good), and the
admin's test console loads the draft.

The provider client is built from the bot's own key (``provider_credentials``, FR-17) when it
has one for its provider, decrypted in memory here; otherwise from the gateway's demo key.
Keys are cached per bot like live versions (``ttl``, dropped on ``bot:published``, which
admin-api also sends when a key changes).

``StaticBotLoader`` serves one flow with one provider: the gateway's tests use it.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.credentials import CredentialCipher
from rozmova_core.models import Bot, BotVersion, ProviderCredential
from rozmova_engine.providers import LLMProvider, ProviderConfig, registry

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BotRuntime:
    flow: FlowGraph
    provider: LLMProvider
    model: str
    bot_version: int | None  # None for a draft
    provider_name: str = "static"  # the bot's provider setting; labels metrics and spans


class BotUnavailableError(Exception):
    """The bot cannot be loaded right now (database or provider down); the call may retry."""


class BotLoader(Protocol):
    async def load(
        self, bot_id: UUID, *, version: int | None = None, draft: bool = False
    ) -> BotRuntime | None:
        """The live version, or ``version``, or the draft; ``None`` if there is none."""
        ...

    async def watch(self) -> None:
        """Runs for the app's lifetime to keep caches fresh; may return at once."""
        ...

    async def aclose(self) -> None: ...


class DemoKeySettings(BaseSettings):
    """``ROZMOVA_DEMO_*``: the LLM key of bots without their own key (ROZ-32)."""

    model_config = SettingsConfigDict(env_prefix="ROZMOVA_DEMO_", extra="ignore")

    provider: str = "openai"  # the provider this key belongs to
    api_key: SecretStr | None = None
    base_url: str | None = None


class Providers:
    """One client per provider name and key for the process, created on first use.

    ``ciphertext`` is a bot's encrypted key; clients are keyed by it, so a bot whose key
    changed gets a new client while calls in flight keep the old one. Old clients stay
    until shutdown: keys change rarely and a client is a connection pool, not a session.
    """

    def __init__(
        self, configs: dict[str, ProviderConfig], cipher: CredentialCipher | None = None
    ) -> None:
        self._configs = configs
        self._cipher = cipher
        self._clients: dict[tuple[str, str | None], LLMProvider] = {}

    @classmethod
    def from_settings(
        cls, settings: DemoKeySettings, cipher: CredentialCipher | None = None
    ) -> "Providers":
        config = ProviderConfig(api_key=settings.api_key, base_url=settings.base_url)
        return cls({settings.provider: config}, cipher)

    def get(self, name: str, ciphertext: str | None = None) -> LLMProvider:
        """Raises ``CredentialError`` when the key cannot be decrypted."""
        if self._cipher is None:
            ciphertext = None  # no master key: every bot runs on the demo key
        key = (name, ciphertext)
        if key not in self._clients:
            config = self._configs.get(name, ProviderConfig())
            if ciphertext is not None and self._cipher is not None:
                config = config.model_copy(update={"api_key": self._cipher.decrypt(ciphertext)})
            self._clients[key] = registry.create(name, config)
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


@dataclass(frozen=True, slots=True)
class _Spec:
    """What a runtime is built from: everything but the provider client."""

    flow: FlowGraph
    provider: str
    model: str
    version: int | None


class PublishedBotLoader:
    def __init__(
        self,
        redis: Redis,
        engine: AsyncEngine,
        providers: Providers,
        *,
        ttl: float = 30.0,
        max_versions: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._engine = engine
        self._db = async_sessionmaker(engine, expire_on_commit=False)
        self._providers = providers
        self._ttl = ttl
        self._clock = clock
        self._live: dict[UUID, tuple[float, _Spec]] = {}
        self._keys: dict[UUID, tuple[float, dict[str, str]]] = {}
        self._versions: OrderedDict[tuple[UUID, int], _Spec] = OrderedDict()
        self._max_versions = max_versions

    async def load(
        self, bot_id: UUID, *, version: int | None = None, draft: bool = False
    ) -> BotRuntime | None:
        try:
            if draft:
                spec = await self._draft(bot_id)
            elif version is not None:
                spec = await self._version(bot_id, version)
            else:
                spec = await self._live_spec(bot_id)
            ciphertext = None if spec is None else (await self._bot_keys(bot_id)).get(spec.provider)
        except (SQLAlchemyError, OSError) as exc:
            log.exception("bot_load_failed", bot_id=str(bot_id))
            raise BotUnavailableError(str(exc)) from exc
        if spec is None:
            return None
        try:
            provider = self._providers.get(spec.provider, ciphertext)
        except Exception as exc:  # a bad key or any SDK error building a client; not BaseException
            log.exception("provider_unavailable", bot_id=str(bot_id), provider=spec.provider)
            raise BotUnavailableError(f"provider {spec.provider}") from exc
        return BotRuntime(spec.flow, provider, spec.model, spec.version, spec.provider)

    def invalidate(self, bot_id: UUID) -> None:
        self._live.pop(bot_id, None)
        self._keys.pop(bot_id, None)

    async def watch(self) -> None:
        """Drop a bot's cached live version when ``bot:published`` names it.

        While not subscribed (Redis down, reconnecting) signals are lost, so the cache is
        cleared on every (re)subscribe; ``ttl`` bounds staleness in between.
        """
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(live_bots.PUBLISHED_CHANNEL)
                    self._clear()
                    while True:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                        if message is not None:
                            self._on_published(message["data"])
            except (RedisError, OSError):
                log.warning("bot_published_watch_failed", exc_info=True)
                self._clear()
                await asyncio.sleep(1.0)

    def _clear(self) -> None:
        self._live.clear()
        self._keys.clear()

    def _on_published(self, data: object) -> None:
        try:
            bot_id = UUID(str(data))
        except ValueError:
            log.warning("bot_published_bad_message", data=str(data)[:100])
            return
        self.invalidate(bot_id)
        log.info("bot_cache_invalidated", bot_id=str(bot_id))

    async def aclose(self) -> None:
        await self._providers.aclose()
        await self._engine.dispose()

    # --- sources ----------------------------------------------------------------------

    async def _live_spec(self, bot_id: UUID) -> _Spec | None:
        cached = self._live.get(bot_id)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        spec = await self._from_redis(bot_id) or await self._live_from_db(bot_id)
        if spec is not None:
            self._live[bot_id] = (self._clock() + self._ttl, spec)
        return spec

    async def _bot_keys(self, bot_id: UUID) -> dict[str, str]:
        """The bot's encrypted keys by provider; decrypted only when a client is built.

        With PostgreSQL down, an expired entry still serves: the live version may come from
        Redis then, and a call should not fail only because its key could not be re-read.
        """
        cached = self._keys.get(bot_id)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        try:
            async with self._db() as db:
                rows = await db.execute(
                    select(ProviderCredential.provider, ProviderCredential.secret_ciphertext).where(
                        ProviderCredential.bot_id == bot_id
                    )
                )
                keys = dict(rows.all())
        except (SQLAlchemyError, OSError):
            if cached is None:
                raise
            log.warning("bot_keys_stale", bot_id=str(bot_id), exc_info=True)
            return cached[1]
        self._keys[bot_id] = (self._clock() + self._ttl, keys)
        return keys

    async def _from_redis(self, bot_id: UUID) -> _Spec | None:
        try:
            live = await live_bots.read(self._redis, bot_id)
        except (RedisError, OSError, ValidationError):
            log.warning("live_cache_read_failed", bot_id=str(bot_id), exc_info=True)
            return None
        if live is None:
            return None
        return _Spec(FlowGraph.model_validate(live.flow), live.provider, live.model, live.version)

    async def _live_from_db(self, bot_id: UUID) -> _Spec | None:
        async with self._db() as db:
            row = (
                await db.execute(
                    select(Bot.provider, Bot.model, BotVersion.version, BotVersion.flow)
                    .join(BotVersion, Bot.live_version_id == BotVersion.id)
                    .where(Bot.id == bot_id)
                )
            ).first()
        if row is None:
            return None
        provider, model, version, flow = row
        log.info("bot_loaded_from_db", bot_id=str(bot_id), version=version)
        return _Spec(FlowGraph.model_validate(flow), provider, model, version)

    async def _version(self, bot_id: UUID, version: int) -> _Spec | None:
        key = (bot_id, version)
        if key in self._versions:
            self._versions.move_to_end(key)
            return self._versions[key]
        async with self._db() as db:
            row = (
                await db.execute(
                    select(Bot.provider, Bot.model, BotVersion.flow)
                    .join(BotVersion, BotVersion.bot_id == Bot.id)
                    .where(Bot.id == bot_id, BotVersion.version == version)
                )
            ).first()
        if row is None:
            return None
        provider, model, flow = row
        spec = _Spec(FlowGraph.model_validate(flow), provider, model, version)
        self._versions[key] = spec
        if len(self._versions) > self._max_versions:
            self._versions.popitem(last=False)
        return spec

    async def _draft(self, bot_id: UUID) -> _Spec | None:
        async with self._db() as db:
            bot = await db.get(Bot, bot_id)
        if bot is None:
            return None
        return _Spec(FlowGraph.model_validate(bot.draft_flow or {}), bot.provider, bot.model, None)


class StaticBotLoader:
    """Every bot id and version gets the same flow and one provider client."""

    def __init__(self, flow: FlowGraph, provider: LLMProvider, model: str) -> None:
        self._runtime = BotRuntime(flow=flow, provider=provider, model=model, bot_version=1)

    async def load(
        self, bot_id: UUID, *, version: int | None = None, draft: bool = False
    ) -> BotRuntime | None:
        return self._runtime

    async def watch(self) -> None:
        return None

    async def aclose(self) -> None:
        close = getattr(self._runtime.provider, "aclose", None)
        if close is not None:
            await close()
