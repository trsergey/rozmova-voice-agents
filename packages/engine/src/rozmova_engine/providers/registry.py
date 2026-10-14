"""Provider registry keyed by ``bots.provider`` (FR-16).

The gateway reads the bot's provider name and decrypted credentials at session start and
asks the registry for an ``LLMProvider``. Adapters register a factory under each name
they serve, e.g. ``openai``, ``azure_openai`` and ``openrouter`` share one adapter class.
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, SecretStr

from rozmova_engine.providers.base import LLMProvider


class ProviderConfig(BaseModel):
    """Connection settings for one bot's provider, built from ``provider_credentials``.

    For Azure OpenAI the deployment name is passed as ``model`` to ``stream()``, so only
    the endpoint and ``api_version`` differ here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # SecretStr keeps the key out of reprs and logs.
    api_key: SecretStr | None = None
    base_url: str | None = None
    api_version: str | None = None


ProviderFactory = Callable[[ProviderConfig], LLMProvider]


class UnknownProviderError(LookupError):
    """No factory is registered under the bot's provider name."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if name in self._factories:
            raise ValueError(f"provider {name!r} is already registered")
        self._factories[name] = factory

    def create(self, name: str, config: ProviderConfig) -> LLMProvider:
        try:
            factory = self._factories[name]
        except KeyError:
            known = ", ".join(sorted(self._factories)) or "none"
            raise UnknownProviderError(f"unknown provider {name!r} (known: {known})") from None
        return factory(config)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._factories)


# Filled by the adapters (ROZ-22, ROZ-23); tests build their own registry.
registry = ProviderRegistry()
