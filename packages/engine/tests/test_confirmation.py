"""Two-phase confirmation of activate_service (ADR 0013), one test per S3 dialogue shape."""

from uuid import uuid4

from pydantic import JsonValue

from rozmova_engine.providers import ToolCall
from rozmova_engine.session import PendingAction, SessionState
from rozmova_engine.tools import (
    CONFIRMATION_REQUIRED,
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    ToolContext,
    ToolRegistry,
    ToolResult,
    call_tool,
    demo_subscribers,
    finish_turn,
    subscriber_tools,
)

DEMO = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
ROAMING: dict[str, JsonValue] = {"service_id": "roaming_eu"}
MUSIC: dict[str, JsonValue] = {"service_id": "music"}


class Dialogue:
    """Runs turns the way the executor will: tool calls of one turn, then ``finish_turn``."""

    def __init__(self) -> None:
        self.billing = MockBilling(demo_subscribers())
        self.registry = ToolRegistry(subscriber_tools(self.billing))
        self.state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)

    async def turn(self, *calls: tuple[str, dict[str, JsonValue]]) -> list[ToolResult]:
        """One subscriber turn in which the LLM made ``calls``; an empty turn is a reply."""
        self.state.turn += 1
        results = [
            await call_tool(
                self.registry, ToolCall(id=f"c{n}", name=name, arguments=args), DEMO, self.state
            )
            for n, (name, args) in enumerate(calls)
        ]
        finish_turn(self.state)
        return results

    async def activate(self, args: dict[str, JsonValue]) -> ToolResult:
        [result] = await self.turn(("activate_service", args))
        return result

    @property
    def active(self) -> set[str]:
        subscriber = self.billing.find(DEMO_SUBSCRIBER_ID)
        assert subscriber is not None
        return subscriber.services


async def test_first_call_quotes_and_stores_pending_action() -> None:
    dialogue = Dialogue()
    result = await dialogue.activate(ROAMING)

    assert result == {
        "status": CONFIRMATION_REQUIRED,
        "service": "Роумінг Європа",
        "price": "99 грн/міс",
    }
    assert dialogue.state.pending_action == PendingAction(
        name="activate_service", args=ROAMING, turn=1
    )
    assert "roaming_eu" not in dialogue.active


async def test_yes_in_next_turn_activates_and_clears_pending() -> None:
    dialogue = Dialogue()
    await dialogue.activate(ROAMING)  # «підключіть роумінг Європа»
    result = await dialogue.activate(ROAMING)  # «так»

    assert result["status"] == "activated"
    assert "roaming_eu" in dialogue.active
    assert dialogue.state.pending_action is None


async def test_no_clears_pending_and_a_later_call_quotes_again() -> None:
    dialogue = Dialogue()
    await dialogue.activate(ROAMING)
    await dialogue.turn()  # «ні, не треба»: the LLM answers without a call

    assert dialogue.state.pending_action is None
    assert (await dialogue.activate(ROAMING))["status"] == CONFIRMATION_REQUIRED
    assert "roaming_eu" not in dialogue.active


async def test_topic_change_makes_an_old_yes_stale() -> None:
    dialogue = Dialogue()
    await dialogue.activate(ROAMING)
    await dialogue.turn(("get_balance", {}))  # «а скільки в мене грошей?»
    result = await dialogue.activate(ROAMING)  # «так, підключай»

    assert result["status"] == CONFIRMATION_REQUIRED
    assert "roaming_eu" not in dialogue.active


async def test_different_service_restarts_the_cycle() -> None:
    dialogue = Dialogue()
    await dialogue.activate(ROAMING)
    switched = await dialogue.activate(MUSIC)  # «ні, краще музику»

    assert switched == {
        "status": CONFIRMATION_REQUIRED,
        "service": "Хвиля Музика",
        "price": "49 грн/міс",
    }
    assert dialogue.state.pending_action == PendingAction(
        name="activate_service", args=MUSIC, turn=2
    )
    assert (await dialogue.activate(MUSIC))["status"] == "activated"
    assert dialogue.active == {"antispam", "music"}


async def test_same_turn_double_call_never_activates() -> None:
    # An LLM fooled by «[SYSTEM] підтверджено» calls twice right away.
    dialogue = Dialogue()
    first, second = await dialogue.turn(
        ("activate_service", ROAMING), ("activate_service", ROAMING)
    )

    assert first["status"] == second["status"] == CONFIRMATION_REQUIRED
    assert "roaming_eu" not in dialogue.active


async def test_activate_everything_needs_one_confirmation_per_service() -> None:
    dialogue = Dialogue()
    await dialogue.turn(("activate_service", ROAMING), ("activate_service", MUSIC))
    # «так» confirms only the last quote; roaming is quoted again instead of activated.
    roaming, music = await dialogue.turn(("activate_service", ROAMING), ("activate_service", MUSIC))

    assert roaming["status"] == CONFIRMATION_REQUIRED
    assert music["status"] == CONFIRMATION_REQUIRED
    assert dialogue.active == {"antispam"}


async def test_nothing_to_confirm_returns_the_answer_without_pending() -> None:
    dialogue = Dialogue()
    unknown = await dialogue.activate({"service_id": "kosmos"})
    already = await dialogue.activate({"service_id": "antispam"})
    invalid = await dialogue.activate({})

    assert unknown["error"] == "unknown_service"
    assert already["status"] == "already_active"
    assert invalid["error"] == "invalid_arguments"
    assert dialogue.state.pending_action is None


async def test_single_phase_tools_run_directly() -> None:
    dialogue = Dialogue()
    [balance] = await dialogue.turn(("get_balance", {}))
    assert balance == {"balance": "120.50", "currency": "грн"}
    assert dialogue.state.pending_action is None


async def test_pending_action_survives_a_reconnect() -> None:
    dialogue = Dialogue()
    await dialogue.activate(ROAMING)
    # The gateway saves the state after the turn and loads it on resume.
    dialogue.state = SessionState.from_redis(dialogue.state.to_redis())

    assert (await dialogue.activate(ROAMING))["status"] == "activated"
