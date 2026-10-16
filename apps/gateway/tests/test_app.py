"""End to end over a real WebSocket through FastAPI's TestClient."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import websockets
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.asyncio import Redis
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.bots import BotLoader, BotRuntime, BotUnavailableError, StaticBotLoader
from gateway.calls import Calls
from rozmova_contracts.flow import FlowGraph
from rozmova_core import tickets
from rozmova_core.call_events import STREAM
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_engine.providers import TextDelta, ToolCall
from rozmova_engine.providers.fake import Delay, FakeProvider, text_turn
from rozmova_engine.rag.answer import KnowledgeSearch
from rozmova_engine.rag.search import SearchHit, SearchResult
from rozmova_engine.session import INTERRUPTED_MARK

FLOW = FlowGraph.model_validate_json(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)
SECRET = "test-secret"  # noqa: S105 - a test-only HMAC key
SETTINGS = Settings(ticket_secret=SecretStr(SECRET), allowed_origins=["https://demo.example"])


class Gateway:
    """A gateway app plus the admin API's half: issuing tickets.

    ``redis`` makes a client per event loop (the app and the test run on different
    loops); by default all of them share one in-memory server.
    """

    def __init__(
        self,
        loader: BotLoader,
        redis: Callable[[], Redis] | None = None,
        calls: Calls | None = None,
        kb: KnowledgeSearch | None = None,
    ) -> None:
        server = FakeServer()
        self.redis = redis or (lambda: FakeAsyncRedis(server=server, decode_responses=True))
        self.app = create_app(loader, self.redis(), SETTINGS, calls, kb)

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
    async def load(self, bot_id: object, **_: object) -> BotRuntime | None:
        return None

    async def watch(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def test_unknown_bot_is_refused() -> None:
    gateway = Gateway(NoBots())
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        assert refused_code(http, url(bot_id, call_id, gateway.ticket(bot_id, call_id))) == 4404


def test_gateway_needs_a_ticket_secret() -> None:
    with pytest.raises(RuntimeError, match="ROZMOVA_TICKET_SECRET"):
        create_app(NoBots(), FakeAsyncRedis(), Settings(ticket_secret=None))


# --- call events (ROZ-52) ---------------------------------------------------------------


def call_and_read_stream(gateway: Gateway) -> tuple[UUID, list[dict[str, str]]]:
    """A call with one balance turn and a hangup; the app's shutdown flushes the events."""
    bot_id, call_id = uuid4(), uuid4()
    ticket = gateway.ticket(bot_id, call_id)
    with (
        TestClient(gateway.app) as http,
        http.websocket_connect(url(bot_id, call_id, ticket)) as ws,
    ):
        while ws.receive_json()["type"] != "turn_end":
            pass
        ws.send_json({"type": "user_text", "text": "баланс"})
        while ws.receive_json()["type"] != "turn_end":
            pass
        ws.send_json({"type": "hangup"})
        while ws.receive_json()["type"] != "session_end":
            pass

    async def read() -> list[dict[str, str]]:
        redis = gateway.redis()
        try:
            raw: Any = await redis.xrange(STREAM)
            return [fields for _, fields in raw]
        finally:
            await redis.aclose()

    return call_id, asyncio.run(read())


def test_a_call_is_written_to_the_call_events_stream(gateway: Gateway) -> None:
    call_id, entries = call_and_read_stream(gateway)

    assert {e["call_id"] for e in entries} == {str(call_id)}
    types = [e["type"] for e in entries]
    assert types[0] == "session_started"
    assert types[-2:] == ["session_end", "session_closed"]
    assert {"user_message", "tool_call", "tool_result", "assistant_message"} <= set(types)
    started = json.loads(entries[0]["payload"])
    assert started["user_id"] == "viewer-1"
    assert started["debug"] is False


@pytest.mark.integration
def test_call_events_reach_a_real_redis_capped(redis_url: str) -> None:
    """The pipelined ``XADD … MAXLEN ~`` against a real server, not only fakeredis."""
    gateway = Gateway(
        StaticBotLoader(FLOW, balance_provider(), "gpt-4.1-mini"),
        redis=lambda: create_redis(Settings(redis_url=SecretStr(redis_url))),
    )
    call_id, entries = call_and_read_stream(gateway)

    mine = [e for e in entries if e["call_id"] == str(call_id)]
    assert [e["type"] for e in mine][-1] == "session_closed"
    assert all(json.loads(e["payload"]) is not None for e in mine)


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


# --- bot versions (ROZ-51) ----------------------------------------------------------------


def with_greeting(greeting: str) -> FlowGraph:
    raw = FLOW.model_dump(mode="json")
    [start] = [n for n in raw["nodes"] if n["type"] == "start"]
    start["data"]["greeting"] = greeting
    return FlowGraph.model_validate(raw)


class VersionedBots:
    """A bot whose live version the test switches; any version or the draft on request.
    Each flow greets with its own name, so the greeting shows what the call runs."""

    def __init__(self) -> None:
        self.live = 1
        self.unavailable = False
        self.provider = FakeProvider([])
        self.loads: list[tuple[int | None, bool]] = []  # (version, draft) per load

    async def load(
        self, bot_id: UUID, *, version: int | None = None, draft: bool = False
    ) -> BotRuntime | None:
        self.loads.append((version, draft))
        if self.unavailable:
            raise BotUnavailableError("database down")
        if draft:
            return BotRuntime(with_greeting("чернетка"), self.provider, "m", None)
        number = version or self.live
        return BotRuntime(with_greeting(f"версія {number}"), self.provider, "m", number)

    async def watch(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def start_call(ws: Any) -> tuple[dict[str, Any], str]:
    """``session_started`` and the greeting text."""
    started = ws.receive_json()
    greeting = "".join(e["text"] for e in until_turn_end(ws) if e["type"] == "assistant_delta")
    return started, greeting


def test_reconnect_keeps_the_version_the_call_started_on() -> None:
    bots = VersionedBots()
    gateway = Gateway(bots)
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        with http.websocket_connect(url(bot_id, call_id, gateway.ticket(bot_id, call_id))) as ws:
            first, greeting = start_call(ws)
        assert (first["bot_version"], greeting) == (1, "версія 1")

        bots.live = 2  # published while the call was away
        bots.loads.clear()
        with http.websocket_connect(url(bot_id, call_id, gateway.ticket(bot_id, call_id))) as ws:
            resumed = ws.receive_json()
        assert (resumed["resumed"], resumed["bot_version"]) == (True, 1)
        # The live version (2) was loaded first, then the call's own version 1 for the run.
        assert bots.loads == [(None, False), (1, False)]

        other_call = uuid4()
        path = url(bot_id, other_call, gateway.ticket(bot_id, other_call))
        with http.websocket_connect(path) as ws:
            new, greeting = start_call(ws)
        assert (new["bot_version"], greeting) == (2, "версія 2")


def test_draft_needs_a_debug_ticket() -> None:
    gateway = Gateway(VersionedBots())
    bot_id = uuid4()
    with TestClient(gateway.app) as http:
        call_id = uuid4()
        plain = url(bot_id, call_id, gateway.ticket(bot_id, call_id)) + "&draft=true"
        assert refused_code(http, plain) == 4401

        call_id = uuid4()
        debug = url(bot_id, call_id, gateway.ticket(bot_id, call_id, debug=True)) + "&draft=true"
        with http.websocket_connect(debug) as ws:
            started, greeting = start_call(ws)

    assert (started["bot_version"], greeting) == (None, "чернетка")


def test_bot_that_cannot_load_is_4503() -> None:
    bots = VersionedBots()
    bots.unavailable = True
    gateway = Gateway(bots)
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        assert refused_code(http, url(bot_id, call_id, gateway.ticket(bot_id, call_id))) == 4503


# --- drain for a shutdown (FR-21) -------------------------------------------------------


def test_new_calls_get_503_while_draining() -> None:
    calls = Calls()
    gateway = Gateway(StaticBotLoader(FLOW, balance_provider(), "gpt-4.1-mini"), calls=calls)
    bot_id, call_id = uuid4(), uuid4()
    with TestClient(gateway.app) as http:
        http.portal.call(calls.drain, 0.1)  # type: ignore[union-attr]
        with (
            pytest.raises(WebSocketDenialResponse) as info,
            http.websocket_connect(url(bot_id, call_id, gateway.ticket(bot_id, call_id))),
        ):
            pass
    assert info.value.status_code == 503


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.integration
async def test_sigterm_drains_calls_and_exits_0(redis_url: str) -> None:
    """A real process: SIGTERM mid-reply, the reply finishes, the call ends with
    ``session_end{shutdown}`` and 1012, a new call gets 503, the process exits 0."""
    port = free_port()
    env = os.environ | {
        "ROZMOVA_TICKET_SECRET": SECRET,
        "ROZMOVA_REDIS_URL": redis_url,
        "ROZMOVA_LOG_JSON": "true",
    }
    launcher = Path(__file__).with_name("slow_llm_gateway.py")
    proc = subprocess.Popen(  # noqa: ASYNC220, S603 - the test's own launcher
        [sys.executable, str(launcher), str(port)], env=env, stdout=subprocess.DEVNULL
    )
    redis = create_redis(Settings(redis_url=SecretStr(redis_url)))
    try:
        async with asyncio.timeout(20):  # until the server listens
            while True:
                with contextlib.suppress(OSError):
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    break
                await asyncio.sleep(0.1)

        async def ws_url() -> tuple[UUID, str]:
            bot_id, call_id = uuid4(), uuid4()
            token = await tickets.issue(
                redis, SECRET, user_id="viewer-1", bot_id=bot_id, call_id=call_id
            )
            return call_id, f"ws://127.0.0.1:{port}{url(bot_id, call_id, token)}"

        call_id, first = await ws_url()
        _, second = await ws_url()
        events: list[dict[str, Any]] = []
        async with websockets.connect(first) as ws:
            while not events or events[-1]["type"] != "turn_end":  # the greeting
                events.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "user_text", "text": "баланс"}))
            assert json.loads(await ws.recv())["text"] == "Зараз подивлюсь"

            proc.send_signal(signal.SIGTERM)
            await asyncio.sleep(0.3)  # the server notices the signal on its next tick
            with pytest.raises(websockets.InvalidStatus) as refused:
                await websockets.connect(second)
            assert refused.value.response.status_code == 503

            with contextlib.suppress(websockets.ConnectionClosed):
                async for raw in ws:
                    events.append(json.loads(raw))
            assert ws.close_code == 1012
        assert [e["type"] for e in events[-3:]] == ["assistant_delta", "turn_end", "session_end"]
        assert events[-1]["reason"] == "shutdown"
        assert await redis.exists(f"session:{call_id}") == 1  # resumable on the new instance

        assert await asyncio.to_thread(proc.wait, 15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        await redis.aclose()


@pytest.mark.integration
async def test_logs_of_a_call_never_show_the_ticket(redis_url: str, tmp_path: Path) -> None:
    """uvicorn logs every WebSocket with its URL (ROZ-78): the line keeps the path and
    ``call_id``, the ticket is masked. A real process, since TestClient has no uvicorn."""
    port = free_port()
    env = os.environ | {
        "ROZMOVA_TICKET_SECRET": SECRET,
        "ROZMOVA_REDIS_URL": redis_url,
        "ROZMOVA_LOG_JSON": "true",
    }
    launcher = Path(__file__).with_name("slow_llm_gateway.py")
    log_path = tmp_path / "gateway.log"
    redis = create_redis(Settings(redis_url=SecretStr(redis_url)))
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(  # noqa: ASYNC220, S603 - the test's own launcher
            [sys.executable, str(launcher), str(port)], env=env, stdout=log_file
        )
        try:
            async with asyncio.timeout(20):  # until the server listens
                while True:
                    with contextlib.suppress(OSError):
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        break
                    await asyncio.sleep(0.1)

            bot_id, call_id = uuid4(), uuid4()
            token = await tickets.issue(
                redis, SECRET, user_id="viewer-1", bot_id=bot_id, call_id=call_id
            )
            async with websockets.connect(
                f"ws://127.0.0.1:{port}{url(bot_id, call_id, token)}"
            ) as ws:
                while json.loads(await ws.recv())["type"] != "turn_end":  # the greeting
                    pass
                await ws.send(json.dumps({"type": "hangup"}))
                while json.loads(await ws.recv())["type"] != "session_end":
                    pass

            proc.send_signal(signal.SIGTERM)
            assert await asyncio.to_thread(proc.wait, 15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
            await redis.aclose()

    output = log_path.read_text(encoding="utf-8")
    websocket_lines = [line for line in output.splitlines() if "WebSocket /ws/calls/" in line]
    assert websocket_lines, output
    assert f"call_id={call_id}" in websocket_lines[0]
    assert "ticket=***" in websocket_lines[0]
    assert token not in output
    # A ticket is `<payload>.<signature>`; neither half may leak on its own.
    assert all(part not in output for part in token.split("."))


# --- demo limits ------------------------------------------------------------------------


def test_a_user_gets_at_most_3_open_calls(gateway: Gateway) -> None:
    bot_id = uuid4()

    def connect(http: TestClient) -> Any:
        call_id = uuid4()
        return http.websocket_connect(url(bot_id, call_id, gateway.ticket(bot_id, call_id)))

    with TestClient(gateway.app) as http, contextlib.ExitStack() as open_calls:
        for _ in range(3):
            ws = open_calls.enter_context(connect(http))
            assert ws.receive_json()["type"] == "session_started"

        with connect(http) as fourth:
            assert fourth.receive_json() == {"type": "session_end", "reason": "limit"}

        open_calls.close()  # the three calls end and free their places
        with connect(http) as fifth:
            assert fifth.receive_json()["type"] == "session_started"


# --- S2: answers from the knowledge base (ROZ-43) -----------------------------------------

BEZLIM_CHUNK = SearchHit(
    chunk_id=UUID(int=1),
    document_id=UUID(int=2),
    document_title="Тариф «Безлім»",
    source_path="tariffs/bezlim.md",
    section="Вартість тарифу «Безлім»",
    text="# Тариф «Безлім»\n\n## Вартість тарифу «Безлім»\n\n395 грн на місяць.",
    score=0.03,
    similarity=0.6,
    vector_rank=1,
    text_rank=1,
)


class OneChunkKB:
    """Finds the «Безлім» chunk for questions that name it; nothing otherwise."""

    def __init__(self) -> None:
        self.closed = False

    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        found = "безлім" in query.lower()
        return SearchResult(
            query=query, hits=[BEZLIM_CHUNK] if found else [], best_similarity=0.6 if found else 0.2
        )

    async def aclose(self) -> None:
        self.closed = True


def tariff_call(provider: FakeProvider, question: str) -> list[dict[str, Any]]:
    """S2 on the demo flow: `assist` routes to `tariff_kb`; returns the question's events."""
    kb = OneChunkKB()
    gateway = Gateway(StaticBotLoader(FLOW, provider, "gpt-4.1-mini"), kb=kb)
    bot_id, call_id = uuid4(), uuid4()
    path = url(bot_id, call_id, gateway.ticket(bot_id, call_id, debug=True))
    with TestClient(gateway.app) as http, http.websocket_connect(path) as ws:
        ws.receive_json()
        until_turn_end(ws)
        ws.send_json({"type": "user_text", "text": question})
        events = until_turn_end(ws)
        ws.send_json({"type": "hangup"})
        ws.receive_json()
    assert kb.closed
    return events


def test_tariff_question_is_answered_from_the_knowledge_base_with_sources() -> None:
    route = [ToolCall(id="r1", name="route", arguments={"name": "tariff"})]
    provider = FakeProvider([route, text_turn("«Безлім» коштує 395 грн на місяць [1].")])

    events = tariff_call(provider, "скільки коштує Безлім?")

    nodes = [e["node_id"] for e in events if e["type"] == "node_enter"]
    assert nodes == ["assist", "tariff_kb"]  # back to `assist`, which waits silently
    said = "".join(e["text"] for e in events if e["type"] == "assistant_delta")
    assert said == "«Безлім» коштує 395 грн на місяць [1]."
    assert events[-1]["sources"] == [
        {
            "marker": 1,
            "chunk_id": str(UUID(int=1)),
            "document_id": str(UUID(int=2)),
            "document_title": "Тариф «Безлім»",
            "section": "Вартість тарифу «Безлім»",
        }
    ]
    # The second LLM call got the numbered chunk.
    assert "[1] # Тариф «Безлім»" in provider.calls[1].messages[1].content


def test_unknown_tariff_gets_the_not_found_reply() -> None:
    route = [ToolCall(id="r1", name="route", arguments={"name": "tariff"})]
    provider = FakeProvider([route])

    events = tariff_call(provider, "що входить у тариф Космос?")

    said = "".join(e["text"] for e in events if e["type"] == "assistant_delta")
    assert said.startswith("У базі знань немає відповіді")
    assert events[-1]["sources"] == []
    assert len(provider.calls) == 1  # no LLM call for the answer
