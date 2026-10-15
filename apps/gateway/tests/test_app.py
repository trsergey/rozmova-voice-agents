"""End to end over a real WebSocket through FastAPI's TestClient."""

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.bots import BotLoader, BotRuntime, StaticBotLoader
from rozmova_contracts.flow import FlowGraph
from rozmova_core import tickets
from rozmova_core.settings import Settings
from rozmova_engine.providers import ToolCall
from rozmova_engine.providers.fake import FakeProvider, text_turn

FLOW = FlowGraph.model_validate_json(
    (
        Path(__file__).resolve().parents[3] / "packages/contracts/tests/fixtures/demo_flow.json"
    ).read_text(encoding="utf-8")
)
SECRET = "test-secret"  # noqa: S105 - a test-only HMAC key
SETTINGS = Settings(ticket_secret=SecretStr(SECRET), allowed_origins=["https://demo.example"])


class Gateway:
    """A gateway app over an in-memory Redis, plus the admin API's half: issuing tickets."""

    def __init__(self, loader: BotLoader) -> None:
        self.server = FakeServer()
        self.app = create_app(loader, self.redis(), SETTINGS)

    def redis(self) -> FakeAsyncRedis:
        return FakeAsyncRedis(server=self.server, decode_responses=True)

    def ticket(self, bot_id: UUID, call_id: UUID, *, debug: bool = False) -> str:
        async def issue() -> str:
            redis = self.redis()
            try:
                return await tickets.issue(
                    redis, SECRET, user_id="viewer-1", bot_id=bot_id, call_id=call_id, debug=debug
                )
            finally:
                await redis.aclose()

        return asyncio.run(issue())


def balance_provider() -> FakeProvider:
    return FakeProvider([[ToolCall(id="c1", name="get_balance")], text_turn("120,50 грн.")])


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(StaticBotLoader(FLOW, balance_provider(), "gpt-4.1-mini"))


def url(bot_id: UUID, call_id: UUID | None = None, ticket: str | None = None) -> str:
    query = "&".join(
        f"{key}={value}" for key, value in (("call_id", call_id), ("ticket", ticket)) if value
    )
    return f"/ws/calls/{bot_id}?{query}"


def refused_code(http: TestClient, path: str, headers: dict[str, str] | None = None) -> int:
    """Connect and return the close code; a refused call gets no messages before it."""
    with (
        pytest.raises(WebSocketDisconnect) as info,
        http.websocket_connect(path, headers=headers or {}) as ws,
    ):
        ws.receive_json()
    return info.value.code


def until_turn_end(ws: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while not events or events[-1]["type"] != "turn_end":
        events.append(ws.receive_json())
    return events


def run_balance_call(ws: Any, call_id: UUID) -> list[dict[str, Any]]:
    """Greeting, one balance turn and hangup; returns the balance turn's events."""
    started = ws.receive_json()
    assert (started["type"], started["call_id"]) == ("session_started", str(call_id))
    assert "assistant_delta" in [e["type"] for e in until_turn_end(ws)]  # the greeting

    ws.send_json({"type": "user_text", "text": "баланс"})
    events = until_turn_end(ws)

    ws.send_json({"type": "hangup"})
    assert ws.receive_json() == {"type": "session_end", "reason": "hangup"}
    return events


def test_healthz(gateway: Gateway) -> None:
    with TestClient(gateway.app) as http:
        assert http.get("/healthz").json() == {"status": "ok"}


def test_call_with_a_ticket(gateway: Gateway) -> None:
    bot_id, call_id = uuid4(), uuid4()
    path = url(bot_id, call_id, gateway.ticket(bot_id, call_id))
    with TestClient(gateway.app) as http, http.websocket_connect(path) as ws:
        events = run_balance_call(ws, call_id)
    assert "".join(e["text"] for e in events if e["type"] == "assistant_delta") == "120,50 грн."
    assert "tool_call" not in [e["type"] for e in events]  # not a debug ticket


def test_debug_flag_comes_from_the_ticket(gateway: Gateway) -> None:
    bot_id, call_id = uuid4(), uuid4()
    path = url(bot_id, call_id, gateway.ticket(bot_id, call_id, debug=True)) + "&debug=false"
    with TestClient(gateway.app) as http, http.websocket_connect(path) as ws:
        events = run_balance_call(ws, call_id)
    assert "tool_call" in [e["type"] for e in events]


def test_allowed_origin_is_admitted(gateway: Gateway) -> None:
    bot_id, call_id = uuid4(), uuid4()
    path = url(bot_id, call_id, gateway.ticket(bot_id, call_id))
    with (
        TestClient(gateway.app) as http,
        http.websocket_connect(path, headers={"origin": "https://demo.example"}) as ws,
    ):
        assert ws.receive_json()["type"] == "session_started"


def test_ticket_is_single_use(gateway: Gateway) -> None:
    bot_id, call_id = uuid4(), uuid4()
    path = url(bot_id, call_id, gateway.ticket(bot_id, call_id))
    with TestClient(gateway.app) as http:
        with http.websocket_connect(path) as ws:
            assert ws.receive_json()["type"] == "session_started"
        assert refused_code(http, path) == 4401


@pytest.mark.parametrize(
    "case", ["no_ticket", "no_call_id", "forged", "expired", "other_call", "other_bot", "origin"]
)
def test_refused_with_4401(gateway: Gateway, case: str) -> None:
    bot_id, call_id = uuid4(), uuid4()
    token = gateway.ticket(bot_id, call_id)
    headers = None
    match case:
        case "no_ticket":
            path = url(bot_id, call_id)
        case "no_call_id":
            path = url(bot_id, ticket=token)
        case "forged":
            ticket = tickets.verify(token, SECRET)
            path = url(bot_id, call_id, tickets.sign(ticket, "attacker-secret"))
        case "expired":
            ticket = tickets.verify(token, SECRET).model_copy(update={"exp": int(time.time())})
            path = url(bot_id, call_id, tickets.sign(ticket, SECRET))
        case "other_call":
            path = url(bot_id, uuid4(), token)
        case "other_bot":
            path = url(uuid4(), call_id, token)
        case "origin":
            path = url(bot_id, call_id, token)
            headers = {"origin": "https://evil.example"}

    with TestClient(gateway.app) as http:
        assert refused_code(http, path, headers) == 4401
        if case in {"other_call", "other_bot", "origin"}:
            # A refused ticket is not burnt: the rightful connection still gets in.
            with http.websocket_connect(url(bot_id, call_id, token)) as ws:
                assert ws.receive_json()["type"] == "session_started"


class NoBots:
    async def load(self, bot_id: object) -> BotRuntime | None:
        return None


def test_unknown_bot_is_refused() -> None:
    gateway = Gateway(NoBots())
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        assert refused_code(http, url(bot_id, call_id, gateway.ticket(bot_id, call_id))) == 4404


def test_gateway_needs_a_ticket_secret() -> None:
    with pytest.raises(RuntimeError, match="ROZMOVA_TICKET_SECRET"):
        create_app(NoBots(), FakeAsyncRedis(), Settings(ticket_secret=None))
