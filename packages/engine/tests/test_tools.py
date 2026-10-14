import asyncio
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from rozmova_engine.providers import ToolCall
from rozmova_engine.tools import (
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    UnknownToolError,
    demo_subscribers,
    subscriber_tools,
)
from rozmova_engine.tools.subscriber import SERVICES, TARIFFS

KB_README = Path(__file__).resolve().parents[3] / "kb" / "README.md"
DEMO = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(subscriber_tools(MockBilling(demo_subscribers())))


async def call(
    registry: ToolRegistry, name: str, ctx: ToolContext = DEMO, **arguments: str
) -> ToolResult:
    return await registry.call(ToolCall(id="c1", name=name, arguments=dict(arguments)), ctx)


def column(result: ToolResult, key: str, field: str) -> list[str]:
    """``field`` of every object in the list ``result[key]``."""
    items = result[key]
    assert isinstance(items, list)
    return [str(item[field]) for item in items if isinstance(item, dict)]


# --- registry --------------------------------------------------------------------


class EchoArgs(BaseModel):
    text: str
    times: int = 1


async def echo(args: EchoArgs, _: ToolContext) -> ToolResult:
    return {"echo": args.text * args.times}


ECHO = Tool("echo", "Repeats text.", EchoArgs, echo)


def test_spec_schema_comes_from_args_model() -> None:
    spec = ToolRegistry([ECHO]).specs(["echo"])[0]

    assert spec.name == "echo"
    assert spec.description == "Repeats text."
    assert spec.parameters["required"] == ["text"]
    assert set(spec.parameters["properties"]) == {"text", "times"}  # type: ignore[arg-type]


def test_specs_keep_requested_order_and_reject_unknown(registry: ToolRegistry) -> None:
    names = ["list_services", "get_balance"]
    assert [spec.name for spec in registry.specs(names)] == names
    with pytest.raises(UnknownToolError):
        registry.specs(["refund"])


def test_duplicate_registration_fails() -> None:
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([ECHO, ECHO])


async def test_valid_call_runs_handler() -> None:
    result = await call(ToolRegistry([ECHO]), "echo", text="ха")
    assert result == {"echo": "ха"}


async def test_unknown_tool_is_a_result_not_an_exception() -> None:
    result = await call(ToolRegistry([ECHO]), "refund")
    assert result["error"] == "unknown_tool"


async def test_invalid_arguments_are_explained_to_the_llm() -> None:
    registry = ToolRegistry([ECHO])
    result = await registry.call(
        ToolCall(id="c1", name="echo", arguments={"times": "багато"}), DEMO
    )

    assert result["error"] == "invalid_arguments"
    assert set(column(result, "details", "field")) == {"text", "times"}


async def test_slow_tool_times_out() -> None:
    async def slow(_: EchoArgs, __: ToolContext) -> ToolResult:
        await asyncio.sleep(10)
        return {}

    registry = ToolRegistry([Tool("slow", "", EchoArgs, slow)], timeout=0.01)
    result = await call(registry, "slow", text="x")
    assert result["error"] == "timeout"


async def test_handler_failure_becomes_tool_failed() -> None:
    async def broken(_: EchoArgs, __: ToolContext) -> ToolResult:
        raise RuntimeError("billing is down")

    result = await call(ToolRegistry([Tool("broken", "", EchoArgs, broken)]), "broken", text="x")
    assert result["error"] == "tool_failed"
    assert "billing is down" not in str(result)


async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def waits(_: EchoArgs, __: ToolContext) -> ToolResult:
        started.set()
        await asyncio.sleep(10)
        return {}

    registry = ToolRegistry([Tool("waits", "", EchoArgs, waits)])
    task = asyncio.create_task(call(registry, "waits", text="x"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- mock subscriber actions -----------------------------------------------------


def test_subscriber_tools_are_the_sdd_set(registry: ToolRegistry) -> None:
    assert registry.names == {
        "get_balance",
        "get_current_tariff",
        "list_services",
        "activate_service",
    }
    assert registry.get("activate_service").side_effect
    assert not registry.get("get_balance").side_effect


async def test_get_balance_of_demo_subscriber(registry: ToolRegistry) -> None:
    assert await call(registry, "get_balance") == {"balance": "120.50", "currency": "грн"}


async def test_get_current_tariff(registry: ToolRegistry) -> None:
    assert await call(registry, "get_current_tariff") == {
        "id": "optimum",
        "name": "Оптимум",
        "price": "235 грн/міс",
    }


async def test_list_services_splits_active_and_available(registry: ToolRegistry) -> None:
    result = await call(registry, "list_services")

    active = column(result, "active", "id")
    available = column(result, "available", "id")
    assert active == ["antispam"]
    assert set(active) | set(available) == set(SERVICES)


async def test_activate_service_changes_billing(registry: ToolRegistry) -> None:
    first = await call(registry, "activate_service", service_id="roaming_eu")
    second = await call(registry, "activate_service", service_id="roaming_eu")
    services = await call(registry, "list_services")

    assert first == {
        "status": "activated",
        "id": "roaming_eu",
        "name": "Роумінг Європа",
        "price": "99 грн/міс",
    }
    assert second["status"] == "already_active"
    assert set(column(services, "active", "id")) == {"roaming_eu", "antispam"}


async def test_activate_unknown_service_lists_known_ids(registry: ToolRegistry) -> None:
    result = await call(registry, "activate_service", service_id="kosmos")
    assert result["error"] == "unknown_service"
    assert result["known_ids"] == list(SERVICES)


async def test_activate_service_requires_service_id(registry: ToolRegistry) -> None:
    result = await call(registry, "activate_service")
    assert result["error"] == "invalid_arguments"


async def test_extra_arguments_are_rejected(registry: ToolRegistry) -> None:
    result = await call(registry, "get_balance", subscriber_id="someone_else")
    assert result["error"] == "invalid_arguments"


async def test_unknown_subscriber(registry: ToolRegistry) -> None:
    result = await call(registry, "get_balance", ToolContext(subscriber_id="nobody"))
    assert result["error"] == "unknown_subscriber"


def test_billing_instances_do_not_share_state() -> None:
    first, second = MockBilling(demo_subscribers()), MockBilling(demo_subscribers())
    subscriber = first.find(DEMO_SUBSCRIBER_ID)
    assert subscriber is not None
    subscriber.services.add("music")
    assert "music" not in second.find(DEMO_SUBSCRIBER_ID).services  # type: ignore[union-attr]


def test_catalog_matches_knowledge_base() -> None:
    readme = KB_README.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (tariff|service) \| `(\w+)` \| ([^|]+?) \| ([^|]+?) \|$", readme, re.M)
    kb = {(kind, id_, name, price) for kind, id_, name, price in rows}
    ours = {("tariff", o.id, o.name, o.price) for o in TARIFFS.values()} | {
        ("service", o.id, o.name, o.price) for o in SERVICES.values()
    }
    assert ours == kb
