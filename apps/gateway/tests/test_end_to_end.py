"""The gateway end to end on real PostgreSQL and Redis (testcontainers), with FakeProvider:
a bot published in the database, a call with a tool turn, a dropped TCP connection, a
resume by ``call_id``, a hangup, and the call's events in the ``call-events`` stream
(ROZ-74). The server is a real uvicorn in the test's event loop: Starlette's TestClient
cancels the app on disconnect instead of delivering it, so it cannot show a real drop."""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
import websockets
from pydantic import SecretStr

from gateway.app import create_app
from gateway.bots import PublishedBotLoader
from rozmova_core import tickets
from rozmova_core.call_events import STREAM
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, BotVersion
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_engine.providers import LLMProvider, ToolCall
from rozmova_engine.providers.fake import FakeProvider, text_turn
from rozmova_engine.providers.keys import Providers

pytestmark = pytest.mark.integration

SECRET = "e2e-secret"  # noqa: S105 - a test-only HMAC key
FLOW: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)


class ScriptedProviders(Providers):
    """Every provider name is the same scripted fake: no SDK client, no network."""

    def __init__(self, fake: FakeProvider) -> None:
        super().__init__({})
        self.fake = fake

    def get(self, name: str, ciphertext: str | None = None) -> LLMProvider:
        return self.fake


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"


async def publish_demo_bot(settings: Settings) -> UUID:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db, db.begin():
            bot = Bot(name="e2e", provider="openai", model="gpt-4.1-mini")
            db.add(bot)
            await db.flush()
            version = BotVersion(bot_id=bot.id, version=1, flow=FLOW)
            db.add(version)
            await db.flush()
            bot.live_version_id = version.id
            return bot.id
    finally:
        await engine.dispose()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def serve(settings: Settings, fake: FakeProvider) -> AsyncIterator[str]:
    """The gateway on uvicorn; yields its ws:// base URL. Leaving runs the app's shutdown."""
    redis = create_redis(settings)
    loader = PublishedBotLoader(redis, create_engine(settings), ScriptedProviders(fake))
    port = free_port()
    config = uvicorn.Config(
        create_app(loader, redis, settings), host="127.0.0.1", port=port, log_config=None
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):  # up to 10 s
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("uvicorn did not start")
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait([task], timeout=10)


async def until(ws: Any, event_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(5):
        while not events or events[-1]["type"] != event_type:
            events.append(json.loads(await ws.recv()))
    return events


async def test_a_published_bot_call_with_a_drop_and_a_resume(
    database_url: SecretStr, redis_url: str
) -> None:
    settings = Settings(
        env="test",
        database_url=database_url,
        redis_url=SecretStr(redis_url),
        ticket_secret=SecretStr(SECRET),
    )
    bot_id = await publish_demo_bot(settings)
    fake = FakeProvider([[ToolCall(id="c1", name="get_balance")], text_turn("120,50 грн.")])
    redis = create_redis(settings)
    call_id = uuid4()

    async def url(base: str) -> str:
        ticket = await tickets.issue(
            redis, SECRET, user_id="code:e2e", bot_id=bot_id, call_id=call_id
        )
        return f"{base}/ws/calls/{bot_id}?call_id={call_id}&ticket={ticket}"

    try:
        async with serve(settings, fake) as base:
            ws = await websockets.connect(await url(base))
            greeting = await until(ws, "turn_end")
            assert greeting[0] == {
                "type": "session_started",
                "call_id": str(call_id),
                "bot_version": 1,
                "resumed": False,
                "history": None,
            }
            await ws.send(json.dumps({"type": "user_text", "text": "скільки в мене грошей"}))
            turn = await until(ws, "turn_end")
            reply = "".join(e["text"] for e in turn if e["type"] == "assistant_delta")
            assert reply == "120,50 грн."
            assert "tool_call" not in {e["type"] for e in turn}  # not a debug ticket
            ws.transport.abort()  # the network is gone: no close frame
            await asyncio.sleep(0.3)  # the server notices the dead connection

            async with websockets.connect(await url(base)) as ws:
                started = json.loads(await ws.recv())
                assert started["type"] == "session_started"
                assert started["resumed"] is True
                assert [m["text"] for m in started["history"]] == [
                    "Вітаю! Я асистент Hvylia Mobile. Чим можу допомогти?",
                    "скільки в мене грошей",
                    "120,50 грн.",
                ]
                await ws.send(json.dumps({"type": "hangup"}))
                ended = await until(ws, "session_end")
                assert ended[-1] == {"type": "session_end", "reason": "hangup"}
        # Leaving `serve` ran the app's shutdown: the call events are flushed.

        raw: Any = await redis.xrange(STREAM)
    finally:
        await redis.aclose()
    entries = [
        {**fields, "payload": json.loads(fields["payload"])}
        for _, fields in raw
        if fields["call_id"] == str(call_id)
    ]
    types = [e["type"] for e in entries]
    assert types.count("session_started") == 2
    assert {"user_message", "tool_call", "tool_result", "assistant_message"} <= set(types)
    closings = [e["payload"] for e in entries if e["type"] == "session_closed"]
    assert closings == [{"code": None, "resumable": True}, {"code": 1000, "resumable": False}]
    assert all(e["bot_id"] == str(bot_id) for e in entries)
