"""Publish and rollback against a migrated PostgreSQL and a fake Redis."""

import asyncio
import copy
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api import publishing
from admin_api.app import create_app
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, BotVersion, User
from rozmova_core.settings import Settings

DEMO_FLOW: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)
ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}


def with_greeting(greeting: str) -> dict[str, Any]:
    """The demo flow with another greeting: valid, and different in logic."""
    flow = copy.deepcopy(DEMO_FLOW)
    [start] = [n for n in flow["nodes"] if n["type"] == "start"]
    start["data"]["greeting"] = greeting
    return flow


@pytest.fixture
async def settings(database_url: SecretStr) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        viewer_hash = hash_password(VIEWER["password"])
        db.add(User(email=VIEWER["email"], password_hash=viewer_hash, role="viewer"))
    await engine.dispose()
    return settings


class Client:
    def __init__(self, settings: Settings, server: FakeServer) -> None:
        app = create_app(
            settings, create_engine(settings), FakeAsyncRedis(server=server, decode_responses=True)
        )
        self.http = TestClient(app)

    def login(self, credentials: dict[str, str]) -> None:
        body = self.http.post("/auth/login", json=credentials).json()
        self.http.headers[CSRF_HEADER] = body["csrf_token"]


class Setup:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.server = FakeServer()
        self.admin = Client(settings, self.server)
        self.viewer = Client(settings, self.server)

    def redis(self) -> FakeAsyncRedis:
        return FakeAsyncRedis(server=self.server, decode_responses=True)

    def new_bot(self, draft: dict[str, Any] | None = None) -> str:
        bot = self.admin.http.post(
            "/bots", json={"name": "Support", "provider": "openai", "model": "gpt-4.1-mini"}
        ).json()
        if draft is not None:
            self.save_draft(bot["id"], draft)
        bot_id: str = bot["id"]
        return bot_id

    def save_draft(self, bot_id: str, flow: dict[str, Any]) -> None:
        assert self.admin.http.put(f"/bots/{bot_id}/draft", json=flow).status_code == 200

    def publish(self, bot_id: str, note: str | None = None) -> Any:
        return self.admin.http.post(f"/bots/{bot_id}/publish", json={"note": note})


@pytest.fixture
def setup(settings: Settings) -> Iterator[Setup]:
    setup = Setup(settings)
    with setup.admin.http, setup.viewer.http:
        setup.admin.login(ADMIN)
        setup.viewer.login(VIEWER)
        yield setup


async def _versions(settings: Settings, bot_id: str) -> int:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db:
        count = await db.scalar(
            select(func.count()).select_from(BotVersion).where(BotVersion.bot_id == UUID(bot_id))
        )
    await engine.dispose()
    return int(count or 0)


@pytest.mark.integration
async def test_publish_creates_v1_and_fills_the_cache(setup: Setup) -> None:
    bot_id = setup.new_bot(DEMO_FLOW)
    redis = setup.redis()
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(live_bots.PUBLISHED_CHANNEL)
        await pubsub.get_message(timeout=1)  # the subscribe confirmation

        response = setup.publish(bot_id, note="first release")

        signal = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert response.status_code == 201, response.text
    version = response.json()
    assert version["version"] == 1
    assert version["note"] == "first release"
    assert version["created_by"] is not None
    assert signal is not None
    assert signal["data"] == bot_id

    live = await live_bots.read(redis, UUID(bot_id))
    assert live is not None
    assert (str(live.version_id), live.version, live.provider) == (version["id"], 1, "openai")
    assert live.flow["nodes"][0]["id"] == DEMO_FLOW["nodes"][0]["id"]

    bot = setup.viewer.http.get(f"/bots/{bot_id}").json()
    assert (bot["live_version_id"], bot["live_version"]) == (version["id"], 1)
    assert setup.viewer.http.get("/bots").json()[0]["live_version"] == 1


@pytest.mark.integration
async def test_publish_edit_publish_rollback(setup: Setup) -> None:
    bot_id = setup.new_bot(DEMO_FLOW)
    assert setup.publish(bot_id).json()["version"] == 1
    setup.save_draft(bot_id, with_greeting("Привіт! Чим допомогти?"))
    assert setup.publish(bot_id).json()["version"] == 2

    rolled = setup.admin.http.post(f"/bots/{bot_id}/rollback/1")

    assert rolled.status_code == 201, rolled.text
    assert rolled.json()["version"] == 3
    assert rolled.json()["note"] == "rollback of v1"
    live = await live_bots.read(setup.redis(), UUID(bot_id))
    assert live is not None
    assert live.version == 3
    # v1's flow is back.
    assert FlowGraph.model_validate(live.flow).same_logic(FlowGraph.model_validate(DEMO_FLOW))
    # The draft is left alone: the author keeps the v2 edits.
    draft = setup.admin.http.get(f"/bots/{bot_id}/draft").json()["flow"]
    assert draft["nodes"][0]["data"]["greeting"] == "Привіт! Чим допомогти?"


@pytest.mark.integration
async def test_invalid_draft_is_not_published(setup: Setup) -> None:
    broken = {**DEMO_FLOW, "edges": [e for e in DEMO_FLOW["edges"] if not e.get("when")]}
    bot_id = setup.new_bot(broken)

    response = setup.publish(bot_id)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "draft_invalid"
    assert {"missing_route_edge"} <= {issue["code"] for issue in error["details"]["errors"]}
    assert all(issue["node_id"] for issue in error["details"]["errors"])
    assert await _versions(setup.settings, bot_id) == 0
    assert await live_bots.read(setup.redis(), UUID(bot_id)) is None


@pytest.mark.integration
async def test_same_logic_twice_is_409(setup: Setup) -> None:
    bot_id = setup.new_bot(DEMO_FLOW)
    setup.publish(bot_id)
    # Only canvas positions changed: nothing new to publish.
    setup.save_draft(
        bot_id, {**DEMO_FLOW, "nodes": [{**n, "ui": {"x": 5, "y": 5}} for n in DEMO_FLOW["nodes"]]}
    )

    again = setup.publish(bot_id)
    to_live = setup.admin.http.post(f"/bots/{bot_id}/rollback/1")

    assert (again.status_code, again.json()["error"]["code"]) == (409, "nothing_to_publish")
    assert to_live.status_code == 409
    assert await _versions(setup.settings, bot_id) == 1


@pytest.mark.integration
def test_unknown_bot_or_version_is_404(setup: Setup) -> None:
    bot_id = setup.new_bot(DEMO_FLOW)
    setup.publish(bot_id)

    missing_bot = setup.publish("00000000-0000-0000-0000-000000000000")
    missing_version = setup.admin.http.post(f"/bots/{bot_id}/rollback/7")

    assert missing_bot.json()["error"]["code"] == "bot_not_found"
    assert missing_version.status_code == 404
    assert missing_version.json()["error"]["code"] == "version_not_found"


@pytest.mark.integration
def test_viewer_cannot_publish_or_roll_back(setup: Setup) -> None:
    bot_id = setup.new_bot(DEMO_FLOW)
    setup.publish(bot_id)

    for response in (
        setup.viewer.http.post(f"/bots/{bot_id}/publish", json={}),
        setup.viewer.http.post(f"/bots/{bot_id}/rollback/1"),
    ):
        assert response.status_code == 403


@pytest.mark.integration
async def test_concurrent_publish_and_rollback_get_consecutive_versions(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Widen the window between reading the live version and inserting the next one, so
    # without the row lock both requests would pick the same number and one would fail.
    live_flow = publishing._live_flow

    async def slow_live_flow(db: AsyncSession, bot: Bot) -> FlowGraph | None:
        await asyncio.sleep(0.3)
        return await live_flow(db, bot)

    monkeypatch.setattr(publishing, "_live_flow", slow_live_flow)
    bot_id = setup.new_bot(DEMO_FLOW)
    setup.publish(bot_id)  # v1
    setup.save_draft(bot_id, with_greeting("Друга версія"))
    setup.publish(bot_id)  # v2
    setup.save_draft(bot_id, with_greeting("Третя версія"))
    # A second admin browser: its own app and connection pool, so the requests really race.
    other = Client(setup.settings, setup.server)
    with other.http:
        other.login(ADMIN)
        with ThreadPoolExecutor(2) as pool:
            publish = pool.submit(setup.publish, bot_id)
            rollback = pool.submit(other.http.post, f"/bots/{bot_id}/rollback/1")
            results = [publish.result(), rollback.result()]

    assert [r.status_code for r in results] == [201, 201]
    assert sorted(r.json()["version"] for r in results) == [3, 4]
    assert await _versions(setup.settings, bot_id) == 4
