"""Provider clients for a bot: its own key or the demo key (FR-17, ROZ-32).

A bot may have its own key per provider (``provider_credentials``, Fernet-encrypted); the
key is decrypted in memory here and never leaves the process. A bot without one runs on the
demo key from ``ROZMOVA_DEMO_*``. The gateway builds clients for live calls; admin-api
builds them for eval runs over a draft (FR-19), so both answer with the same key.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rozmova_core.credentials import CredentialCipher
from rozmova_engine.providers.base import LLMProvider
from rozmova_engine.providers.registry import ProviderConfig, registry


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

    def has_key(self, name: str, ciphertext: str | None = None) -> bool:
        """Whether ``get`` would build a client with a key: the bot's own or the demo key."""
        if ciphertext is not None and self._cipher is not None:
            return True
        return self._configs.get(name, ProviderConfig()).api_key is not None

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
