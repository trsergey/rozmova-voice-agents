"""End to end over a real WebSocket through FastAPI's TestClient."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.bots import BotRuntime, StaticBotLoader
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.providers import ToolCall
from rozmova_engine.providers.fake import FakeProvider, text_turn

FLOW = FlowGraph.model_validate_json(
    (
        Path(__file__).resolve().parents[3] / "packages/contracts/tests/fixtures/demo_flow.json"
    ).read_text(encoding="utf-8")
)


def client(provider: FakeProvider) -> TestClient:
    return TestClient(create_app(StaticBotLoader(FLOW, provider, "gpt-4.1-mini")))


def test_healthz() -> None:
    with client(FakeProvider()) as http:
        assert http.get("/healthz").json() == {"status": "ok"}


def test_call_over_websocket() -> None:
    provider = FakeProvider([[ToolCall(id="c1", name="get_balance")], text_turn("120,50 грн.")])
    call_id = uuid4()
    with (
        client(provider) as http,
        http.websocket_connect(f"/ws/calls/{uuid4()}?call_id={call_id}") as ws,
    ):
        started = ws.receive_json()
        assert started["type"] == "session_started"
        assert started["call_id"] == str(call_id)
        assert ws.receive_json()["type"] == "assistant_delta"
        assert ws.receive_json()["type"] == "turn_end"

        ws.send_json({"type": "user_text", "text": "баланс"})
        events: list[dict[str, Any]] = []
        while not events or events[-1]["type"] != "turn_end":
            events.append(ws.receive_json())
        assert "".join(e["text"] for e in events if e["type"] == "assistant_delta") == (
            "120,50 грн."
        )
        assert "tool_call" not in [e["type"] for e in events]  # not a debug session

        ws.send_json({"type": "hangup"})
        assert ws.receive_json() == {"type": "session_end", "reason": "hangup"}


def test_unknown_bot_is_refused() -> None:
    class NoBots:
        async def load(self, bot_id: object) -> BotRuntime | None:
            return None

    with (
        TestClient(create_app(NoBots())) as http,
        pytest.raises(WebSocketDisconnect) as info,
        http.websocket_connect(f"/ws/calls/{uuid4()}"),
    ):
        pass
    assert info.value.code == 4404
