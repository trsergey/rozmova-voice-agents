"""Where a session gets its bot: flow, LLM provider and model.

``BotLoader`` is the seam: ROZ-51 implements it over the Redis cache of published versions
and PostgreSQL (with drafts for the test console). ``StaticBotLoader`` serves one flow file
with a provider from the environment, so the gateway runs locally before that exists.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rozmova_contracts.flow import FlowGraph
from rozmova_engine.providers import LLMProvider, ProviderConfig, registry


@dataclass(frozen=True, slots=True)
class BotRuntime:
    flow: FlowGraph
    provider: LLMProvider
    model: str
    bot_version: int | None


class BotLoader(Protocol):
    async def load(self, bot_id: UUID) -> BotRuntime | None:
        """The bot's live version, or ``None`` when there is no such bot."""
        ...


class DemoBotSettings(BaseSettings):
    """``ROZMOVA_DEMO_*``: the static bot for local runs."""

    model_config = SettingsConfigDict(env_prefix="ROZMOVA_DEMO_", extra="ignore")

    # Until the demo flow is seeded (ROZ-54), the contracts fixture is the demo flow.
    flow_path: Path = Path("packages/contracts/tests/fixtures/demo_flow.json")
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    api_key: SecretStr | None = None
    base_url: str | None = None


class StaticBotLoader:
    """Every bot id gets the same flow and one shared provider client."""

    def __init__(self, flow: FlowGraph, provider: LLMProvider, model: str) -> None:
        self._runtime = BotRuntime(flow=flow, provider=provider, model=model, bot_version=1)

    @classmethod
    def from_settings(cls, settings: DemoBotSettings) -> "StaticBotLoader":
        flow = FlowGraph.model_validate_json(settings.flow_path.read_text(encoding="utf-8"))
        config = ProviderConfig(api_key=settings.api_key, base_url=settings.base_url)
        return cls(flow, registry.create(settings.provider, config), settings.model)

    @property
    def provider(self) -> LLMProvider:
        return self._runtime.provider

    async def load(self, bot_id: UUID) -> BotRuntime | None:
        return self._runtime
