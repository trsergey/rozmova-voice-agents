"""The call log API against a migrated PostgreSQL: the list, a call's timeline, who sees
what, and the live feed over a fake Redis."""

import asyncio
import json
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis, FakeRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from starlette.websockets import WebSocketDisconnect

from admin_api.app import create_app
from admin_api.call_log import LIVE_CHANNEL
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import AccessCode, Bot, BotVersion, Call, CallEvent, User
from rozmova_core.settings import Settings

pytestmark = pytest.mark.integration

ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
ORIGIN = {"origin": "http://testserver"}


class Client:
    def __init__(self, settings: Settings, server: FakeServer) -> None:
        redis = FakeAsyncRedis(server=server, decode_responses=True)
        self.http = TestClient(create_app(settings, create_engine(settings), redis))
        self.http.__enter__()
        self.id = ""

    def login(self, path: str, body: dict[str, str]) -> None:
        session = self.http.post(path, json=body).json()
        self.http.headers[CSRF_HEADER] = session["csrf_token"]
        self.id = session["user"]["id"]

    def close(self) -> None:
        self.http.__exit__(None, None, None)


class World:
    """Two bots, an admin, a viewer user and an access code; calls are added per test."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = uuid4()
        self.other_bot = uuid4()

    async def seed(self) -> None:
        engine = create_engine(self.settings)
        async with create_session_factory(engine)() as db, db.begin():
            await db.execute(delete(Bot))
            await db.execute(delete(User))
            await db.execute(delete(AccessCode))
            db.add(Bot(id=self.bot, name="Hvylia", provider="openai", model="m"))
            db.add(Bot(id=self.other_bot, name="Other", provider="openai", model="m"))
            await db.flush()
            db.add(BotVersion(bot_id=self.bot, version=3, flow={}))
            db.add(
                User(
                    email=VIEWER["email"],
                    password_hash=hash_password(VIEWER["password"]),
                    role="viewer",
                )
            )
        await engine.dispose()

    async def call(
        self,
        *,
        minutes: int,
        bot: UUID | None = None,
        user: str | None = None,
        code: str | None = None,
        outcome: str | None = "completed",
        events: Sequence[tuple[str, dict[str, Any]]] = (),
    ) -> UUID:
        call_id = uuid4()
        started = T0 + timedelta(minutes=minutes)
        engine = create_engine(self.settings)
        async with create_session_factory(engine)() as db, db.begin():
            version = await db.scalar(
                select(BotVersion.id).where(BotVersion.bot_id == (bot or self.bot))
            )
            db.add(
                Call(
                    id=call_id,
                    bot_id=bot or self.bot,
                    bot_version_id=version,
                    user_id=UUID(user) if user else None,
                    access_code_id=UUID(code) if code else None,
                    started_at=started,
                    ended_at=started + timedelta(minutes=1) if outcome else None,
                    outcome=outcome,
                )
            )
            await db.flush()
            for n, (kind, payload) in enumerate(events):
                ts = started + timedelta(seconds=n)
                db.add(
                    CallEvent(
                        id=f"{int(ts.timestamp() * 1000)}-{n}",
                        call_id=call_id,
                        ts=ts,
                        type=kind,
                        payload=payload,
                    )
                )
        await engine.dispose()
        return call_id


@pytest.fixture
def settings(database_url: SecretStr) -> Settings:
    return Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
    )


@pytest.fixture
def world(settings: Settings) -> World:
    world = World(settings)
    asyncio.run(world.seed())
    return world


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def people(
    world: World, settings: Settings, server: FakeServer
) -> Iterator[tuple[Client, Client, Client]]:
    admin, viewer, guest = (Client(settings, server) for _ in range(3))
    admin.login("/auth/login", ADMIN)
    viewer.login("/auth/login", VIEWER)
    code = admin.http.post("/access-codes", json={"label": "Acme"}).json()["code"]
    guest.login("/auth/access-code", {"code": code})
    try:
        yield admin, viewer, guest
    finally:
        for client in (admin, viewer, guest):
            client.close()


def test_list_newest_first_with_filters(
    world: World, people: tuple[Client, Client, Client]
) -> None:
    admin, _, guest = people
    first = asyncio.run(
        world.call(minutes=0, code=guest.id, events=[("user_message", {"text": "a"})] * 2)
    )
    handoff = asyncio.run(world.call(minutes=5, outcome="handoff"))
    other = asyncio.run(world.call(minutes=10, bot=world.other_bot))
    running = asyncio.run(world.call(minutes=15, outcome=None))

    page = admin.http.get("/calls").json()
    assert [c["id"] for c in page["items"]] == [str(running), str(other), str(handoff), str(first)]
    assert page["next_cursor"] is None
    oldest = page["items"][-1]
    assert (oldest["bot_name"], oldest["bot_version"], oldest["caller"], oldest["turns"]) == (
        "Hvylia",
        3,
        "Acme",
        2,
    )

    def ids(**params: str) -> list[str]:
        return [c["id"] for c in admin.http.get("/calls", params=params).json()["items"]]

    assert ids(bot_id=str(world.other_bot)) == [str(other)]
    assert ids(outcome="handoff") == [str(handoff)]
    assert ids(outcome="open") == [str(running)]
    since, until = (T0 + timedelta(minutes=5)).isoformat(), (T0 + timedelta(minutes=15)).isoformat()
    assert ids(since=since, until=until) == [str(other), str(handoff)]


def test_pages_neither_repeat_nor_skip(world: World, people: tuple[Client, Client, Client]) -> None:
    admin, _, _ = people
    created = [asyncio.run(world.call(minutes=n)) for n in range(5)]

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = admin.http.get("/calls", params=params).json()
        seen += [c["id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(c) for c in reversed(created)]
    assert admin.http.get("/calls", params={"cursor": "nonsense"}).status_code == 422


def test_timeline_in_order(world: World, people: tuple[Client, Client, Client]) -> None:
    admin, _, _ = people
    events: list[tuple[str, dict[str, Any]]] = [
        ("session_started", {"bot_version": 3}),
        ("node_enter", {"node_id": "assist"}),
        ("user_message", {"text": "мій баланс?"}),
        ("tool_call", {"name": "get_balance", "args": {}}),
        ("tool_result", {"name": "get_balance", "result": {"balance": 120}}),
        ("assistant_message", {"text": "На рахунку 120 грн."}),
        ("turn_end", {"timings": {"total_ms": 900, "ttft_ms": 400, "stages": {"llm": 700}}}),
        ("error", {"code": "tool_timeout", "message": "x"}),
        ("session_end", {"reason": "completed"}),
    ]
    call_id = asyncio.run(world.call(minutes=0, events=events))

    call = admin.http.get(f"/calls/{call_id}").json()

    assert [(e["type"], e["payload"]) for e in call["events"]] == [(t, p) for t, p in events]
    assert call["turns"] == 1
    assert admin.http.get(f"/calls/{uuid4()}").status_code == 404


def test_viewers_see_only_their_own_calls(
    world: World, people: tuple[Client, Client, Client]
) -> None:
    _, viewer, guest = people
    guests = asyncio.run(world.call(minutes=0, code=guest.id))
    viewers = asyncio.run(world.call(minutes=1, user=viewer.id))
    someone_else = asyncio.run(world.call(minutes=2))

    assert [c["id"] for c in guest.http.get("/calls").json()["items"]] == [str(guests)]
    assert [c["id"] for c in viewer.http.get("/calls").json()["items"]] == [str(viewers)]
    assert guest.http.get(f"/calls/{guests}").status_code == 200
    assert guest.http.get(f"/calls/{someone_else}").status_code == 404
    assert viewer.http.get(f"/calls/{guests}").status_code == 404


def test_calls_need_a_session(settings: Settings, world: World) -> None:
    http = TestClient(create_app(settings, create_engine(settings), FakeAsyncRedis()))

    assert http.get("/calls").status_code == 401
    assert http.get(f"/calls/{uuid4()}").status_code == 401


def _publish_until_received(
    server: FakeServer, messages: list[dict[str, Any]], stop: threading.Event
) -> None:
    redis = FakeRedis(server=server, decode_responses=True)
    while not stop.is_set():
        for message in messages:
            redis.publish(LIVE_CHANNEL, json.dumps(message))
        stop.wait(0.05)


def test_live_feed_forwards_events_for_admins(
    world: World, people: tuple[Client, Client, Client], server: FakeServer
) -> None:
    admin, _, _ = people
    mine = {"id": "1-0", "call_id": str(uuid4()), "bot_id": str(world.bot), "type": "node_enter"}
    other = {
        "id": "2-0",
        "call_id": str(uuid4()),
        "bot_id": str(world.other_bot),
        "type": "node_enter",
    }
    stop = threading.Event()
    # The subscription starts after the handshake; publish until the feed has delivered.
    publisher = threading.Thread(target=_publish_until_received, args=(server, [other, mine], stop))

    with admin.http.websocket_connect(f"/calls/live?bot_id={world.bot}", headers=ORIGIN) as ws:
        publisher.start()
        try:
            received = [ws.receive_json() for _ in range(2)]
        finally:
            stop.set()
            publisher.join()

    assert received == [mine, mine]


def test_live_feed_refuses_viewers_and_strangers(
    world: World, people: tuple[Client, Client, Client], settings: Settings
) -> None:
    _, viewer, _ = people

    def close_code(client: TestClient, headers: dict[str, str]) -> int:
        with (
            client.websocket_connect("/calls/live", headers=headers) as ws,
            pytest.raises(WebSocketDisconnect) as closed,
        ):
            ws.receive_text()
        return closed.value.code

    assert close_code(viewer.http, ORIGIN) == 4403
    anonymous = TestClient(create_app(settings, create_engine(settings), FakeAsyncRedis()))
    assert close_code(anonymous, ORIGIN) == 4401
    assert close_code(people[0].http, {"origin": "https://evil.example"}) == 4401
