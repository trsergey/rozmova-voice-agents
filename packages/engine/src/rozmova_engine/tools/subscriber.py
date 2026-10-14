"""Mock subscriber actions for the demo operator Hvylia Mobile (scenarios S1–S3).

``MockBilling`` stands in for the operator's billing API: an in-memory set of fictional
subscribers that lives as long as the process, so a service activated in one call is still
active in the next one. Tariff and service ids, names and prices match the catalog in
``kb/README.md`` (a test keeps them in sync).

``activate_service`` here activates immediately; the two-phase confirmation (ROZ-28) is
enforced by the engine around this tool, not inside it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rozmova_engine.tools.registry import Tool, ToolContext, ToolResult, tool_error


@dataclass(frozen=True, slots=True)
class Offer:
    id: str
    name: str
    price: str


TARIFFS = {
    offer.id: offer
    for offer in (
        Offer("start", "Старт", "145 грн/міс"),
        Offer("optimum", "Оптимум", "235 грн/міс"),
        Offer("bezlim", "Безлім", "395 грн/міс"),
        Offer("simeinyi", "Сімейний", "640 грн/міс"),
        Offer("dytiachyi", "Дитячий", "115 грн/міс"),
    )
}

SERVICES = {
    offer.id: offer
    for offer in (
        Offer("roaming_eu", "Роумінг Європа", "99 грн/міс"),
        Offer("roaming_world", "Роумінг Світ", "249 грн/7 днів"),
        Offer("extra_internet_10gb", "Додаткові 10 ГБ", "69 грн"),
        Offer("antispam", "Антиспам", "19 грн/міс"),
        Offer("call_forwarding", "Переадресація", "0 грн"),
        Offer("who_called", "Хто дзвонив", "9 грн/міс"),
        Offer("music", "Хвиля Музика", "49 грн/міс"),
        Offer("voicemail", "Голосова пошта", "15 грн/міс"),
    )
}


@dataclass(slots=True)
class Subscriber:
    id: str
    balance: Decimal
    tariff_id: str
    services: set[str] = field(default_factory=set)


DEMO_SUBSCRIBER_ID = "demo"


def demo_subscribers() -> list[Subscriber]:
    """A fresh copy of the demo dataset; ``demo`` is the subscriber of the SDD walkthrough."""
    return [
        Subscriber(DEMO_SUBSCRIBER_ID, Decimal("120.50"), "optimum", {"antispam"}),
        Subscriber("low_balance", Decimal("3.20"), "start"),
    ]


class MockBilling:
    def __init__(self, subscribers: Iterable[Subscriber]) -> None:
        self._subscribers = {subscriber.id: subscriber for subscriber in subscribers}

    def find(self, subscriber_id: str) -> Subscriber | None:
        return self._subscribers.get(subscriber_id)


# --- tools -----------------------------------------------------------------------

_ARGS_CONFIG = ConfigDict(extra="forbid")


class NoArgs(BaseModel):
    model_config = _ARGS_CONFIG


class ActivateServiceArgs(BaseModel):
    model_config = _ARGS_CONFIG

    service_id: str = Field(description="Код послуги з бази знань, наприклад `roaming_eu`.")


def _offer(offer: Offer) -> ToolResult:
    return {"id": offer.id, "name": offer.name, "price": offer.price}


def _unknown_subscriber(ctx: ToolContext) -> ToolResult:
    return tool_error("unknown_subscriber", f"Subscriber {ctx.subscriber_id!r} not found.")


def subscriber_tools(billing: MockBilling) -> list[Tool[Any]]:
    """The four subscriber actions bound to ``billing``."""

    async def get_balance(_: NoArgs, ctx: ToolContext) -> ToolResult:
        subscriber = billing.find(ctx.subscriber_id)
        if subscriber is None:
            return _unknown_subscriber(ctx)
        # A string keeps the kopecks exact; the LLM reads it as is.
        return {"balance": f"{subscriber.balance:.2f}", "currency": "грн"}

    async def get_current_tariff(_: NoArgs, ctx: ToolContext) -> ToolResult:
        subscriber = billing.find(ctx.subscriber_id)
        if subscriber is None:
            return _unknown_subscriber(ctx)
        return _offer(TARIFFS[subscriber.tariff_id])

    async def list_services(_: NoArgs, ctx: ToolContext) -> ToolResult:
        subscriber = billing.find(ctx.subscriber_id)
        if subscriber is None:
            return _unknown_subscriber(ctx)
        return {
            "active": [_offer(s) for s in SERVICES.values() if s.id in subscriber.services],
            "available": [_offer(s) for s in SERVICES.values() if s.id not in subscriber.services],
        }

    async def activate_service(args: ActivateServiceArgs, ctx: ToolContext) -> ToolResult:
        subscriber = billing.find(ctx.subscriber_id)
        if subscriber is None:
            return _unknown_subscriber(ctx)
        service = SERVICES.get(args.service_id)
        if service is None:
            return tool_error(
                "unknown_service",
                f"Service {args.service_id!r} does not exist.",
                known_ids=list(SERVICES),
            )
        if service.id in subscriber.services:
            return {"status": "already_active", **_offer(service)}
        subscriber.services.add(service.id)
        return {"status": "activated", **_offer(service)}

    return [
        Tool(
            "get_balance",
            "Поточний баланс рахунку абонента в гривнях.",
            NoArgs,
            get_balance,
        ),
        Tool(
            "get_current_tariff",
            "Тариф, на якому зараз абонент: код, назва й ціна.",
            NoArgs,
            get_current_tariff,
        ),
        Tool(
            "list_services",
            "Послуги абонента з цінами: підключені (`active`) і доступні (`available`).",
            NoArgs,
            list_services,
        ),
        Tool(
            "activate_service",
            "Підключає абоненту послугу за її кодом.",
            ActivateServiceArgs,
            activate_service,
            side_effect=True,
        ),
    ]
