"""The demo reset buttons (ROZ-45) against a migrated PostgreSQL and a fake Redis."""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from admin_api.app import create_app
from admin_api.demo_reset import RESET_NOTE
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, BotVersion, Call, CallEvent, User
from rozmova_core.settings import Settings

ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}
DEMO_FLOW_PATH = Path(__file__).resolve().parents[3] / "flows" / "hvylia-demo.json"
SEED = FlowGraph.model_validate(json.loads(DEMO_FLOW_PATH.read_text(encoding="utf-8")))


@pytest.fixture
async def settings(database_url: SecretStr) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
        demo_flow_path=DEMO_FLOW_PATH,
        eval_gate_bypass=True,
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        viewer_hash = hash_password(VIEWER["password"])
        db.add(User(email=VIEWER["email"], password_hash=viewer_hash, role="viewer"))
    await engine.dispose()
    return settings


Client = Callable[[Settings, FakeServer, dict[str, str]], TestClient]


@pytest.fixture
def client() -> Iterator[Client]:
    """Logged-in clients; the lifespan runs, so the seed creates the demo bot."""
    with ExitStack() as stack:

        def make(settings: Settings, server: FakeServer, credentials: dict[str, str]) -> TestClient:
            redis = FakeAsyncRedis(server=server, decode_responses=True)
            app = create_app(settings, create_engine(settings), redis)
            http = stack.enter_context(TestClient(app))
            login = http.post("/auth/login", json=credentials).json()
            http.headers[CSRF_HEADER] = login["csrf_token"]
            return http

        yield make


async def add_calls(settings: Settings, count: int) -> None:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        for n in range(count):
            call = Call(id=uuid.uuid4(), bot_id=settings.demo_bot_id)
            db.add(call)
            await db.flush()
            db.add(CallEvent(id=f"1-{n}", call_id=call.id, ts=datetime.now(UTC), type="turn_end"))
    await engine.dispose()


async def count(settings: Settings, model: type[Call] | type[CallEvent] | type[BotVersion]) -> int:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db:
        total = await db.scalar(select(func.count()).select_from(model))
    await engine.dispose()
    return int(total or 0)


@pytest.mark.integration
def test_viewers_cannot_reset(settings: Settings, client: Client) -> None:
    http = client(settings, FakeServer(), VIEWER)
    for area in ("calls", "sessions", "demo-bot"):
        assert http.post(f"/ops/reset/{area}").status_code == 403


@pytest.mark.integration
async def test_calls_reset_deletes_calls_and_events_only(
    settings: Settings, client: Client
) -> None:
    http = client(settings, FakeServer(), ADMIN)
    await add_calls(settings, 3)

    assert http.post("/ops/reset/calls").json() == {"deleted": 3}
    assert (await count(settings, Call), await count(settings, CallEvent)) == (0, 0)
    assert await count(settings, BotVersion) == 1  # the demo bot is another area


@pytest.mark.integration
async def test_sessions_reset_deletes_session_keys_only(settings: Settings, client: Client) -> None:
    server = FakeServer()
    http = client(settings, server, ADMIN)
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    await redis.set(f"session:{uuid.uuid4()}", "{}")
    await redis.set(f"session:{uuid.uuid4()}", "{}")
    await add_calls(settings, 1)

    assert http.post("/ops/reset/sessions").json() == {"deleted": 2}
    assert [key async for key in redis.scan_iter(match="session:*")] == []
    assert await live_bots.read(redis, settings.demo_bot_id) is not None
    assert await count(settings, Call) == 1


@pytest.mark.integration
async def test_demo_bot_reset_restores_the_seed(settings: Settings, client: Client) -> None:
    server = FakeServer()
    http = client(settings, server, ADMIN)
    bot_url = f"/bots/{settings.demo_bot_id}"
    edited = http.get(f"{bot_url}/draft").json()["flow"]
    edited["nodes"][0]["data"]["greeting"] = "Змінене привітання"
    http.put(f"{bot_url}/draft", json=edited)
    assert http.post(f"{bot_url}/publish", json={"note": "edit"}).status_code == 201
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        bot = await db.get(Bot, settings.demo_bot_id)
        assert bot is not None
        bot.name, bot.model = "Renamed", "gpt-5.6-luna"
    await engine.dispose()
    await add_calls(settings, 2)

    assert http.post("/ops/reset/demo-bot").json() == {"live_version": 3, "republished": True}

    bot_out = http.get(bot_url).json()
    assert (bot_out["name"], bot_out["model"], bot_out["live_version"]) == (
        "Hvylia Mobile demo",
        "gpt-4.1-mini",
        3,
    )
    assert http.get(f"{bot_url}/draft").json()["hash"] == SEED.logic_hash()
    live = await live_bots.read(FakeAsyncRedis(server=server, decode_responses=True), bot.id)
    assert live is not None
    assert (live.version, live.model) == (3, "gpt-4.1-mini")
    assert FlowGraph.model_validate(live.flow).same_logic(SEED)
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db:
        newest = await db.scalar(select(BotVersion).order_by(BotVersion.version.desc()).limit(1))
    await engine.dispose()
    # The seed's own version: a later change of the flow file still reaches the bot.
    assert newest is not None
    assert (newest.note, newest.created_by) == (RESET_NOTE, None)
    assert await count(settings, Call) == 2  # calls are another area

    # Nothing changed since: no new version.
    assert http.post("/ops/reset/demo-bot").json() == {"live_version": 3, "republished": False}
