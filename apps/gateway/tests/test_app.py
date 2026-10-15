"""End to end over a real WebSocket through FastAPI's TestClient."""

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.asyncio import Redis
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.bots import BotLoader, BotRuntime, StaticBotLoader
from rozmova_contracts.flow import FlowGraph
from rozmova_core import tickets
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_engine.providers import TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, text_turn
from rozmova_engine.session import INTERRUPTED_MARK

FLOW = FlowGraph.model_validate_json(
    (
        Path(__file__).resolve().parents[3] / "packages/contracts/tests/fixtures/demo_flow.json"
    ).read_text(encoding="utf-8")
)
SECRET = "test-secret"  # noqa: S105 - a test-only HMAC key
SETTINGS = Settings(ticket_secret=SecretStr(SECRET), allowed_origins=["https://demo.example"])


class Gateway:
    """A gateway app plus the admin API's half: issuing tickets.

    ``redis`` makes a client per event loop (the app and the test run on different
    loops); by default all of them share one in-memory server.
    """

    def __init__(self, loader: BotLoader, redis: Callable[[], Redis] | None = None) -> None:
        server = FakeServer()
        self.redis = redis or (lambda: FakeAsyncRedis(server=server, decode_responses=True))
        self.app = create_app(loader, self.redis(), SETTINGS)

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


# --- reconnect (FR-21) ------------------------------------------------------------------


def reconnect_after_a_drop_mid_turn(gateway: Gateway, provider: FakeProvider) -> None:
    """Drop the socket while a reply streams, reconnect with a new ticket, go on."""
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        path = url(bot_id, call_id, gateway.ticket(bot_id, call_id))
        with http.websocket_connect(path) as ws:
            assert ws.receive_json()["type"] == "session_started"
            until_turn_end(ws)  # the greeting
            ws.send_json({"type": "user_text", "text": "розкажи про тарифи"})
            assert ws.receive_json()["text"] == "Тарифи у нас такі: "
        # Leaving the block closes the socket in the middle of the turn.

        path = url(bot_id, call_id, gateway.ticket(bot_id, call_id))
        with http.websocket_connect(path) as ws:
            started = ws.receive_json()
            ws.send_json({"type": "user_text", "text": "а роумінг?"})
            events = until_turn_end(ws)

    assert started["type"] == "session_started"
    assert started["resumed"] is True
    assert started["call_id"] == str(call_id)
    assert [(m["role"], m["text"], m["interrupted"]) for m in started["history"][1:]] == [
        ("user", "розкажи про тарифи", False),
        ("assistant", "Тарифи у нас такі: ", True),
    ]
    assert "Роумінг" in "".join(e["text"] for e in events if e["type"] == "assistant_delta")
    assert events[-1]["turn_id"] == 3
    seen = provider.calls[-1].messages
    assert any(m.content.endswith(INTERRUPTED_MARK) for m in seen)


def drop_and_reconnect_provider() -> FakeProvider:
    return FakeProvider(
        [[TextDelta(text="Тарифи у нас такі: "), Delay(10)], text_turn("Роумінг є в 40 країнах.")]
    )


def test_reconnect_resumes_the_call() -> None:
    provider = drop_and_reconnect_provider()
    gateway = Gateway(StaticBotLoader(FLOW, provider, "gpt-4.1-mini"))
    reconnect_after_a_drop_mid_turn(gateway, provider)


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


@pytest.mark.integration
def test_reconnect_resumes_the_call_on_real_redis(redis_url: str) -> None:
    provider = drop_and_reconnect_provider()
    settings = Settings(redis_url=SecretStr(redis_url))
    gateway = Gateway(
        StaticBotLoader(FLOW, provider, "gpt-4.1-mini"), redis=lambda: create_redis(settings)
    )
    reconnect_after_a_drop_mid_turn(gateway, provider)
