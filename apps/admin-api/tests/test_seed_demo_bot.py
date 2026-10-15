"""The demo bot admin-api seeds on start, against a migrated PostgreSQL."""

import asyncio
import json
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from admin_api.app import create_app
from admin_api.seed import REPUBLISHED_NOTE, seed_demo_bot
from admin_api.sessions import CSRF_HEADER
from rozmova_contracts.flow import FlowGraph
from rozmova_core import live_bots
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, BotVersion, User
from rozmova_core.settings import Settings

ADMIN = {"email": "admin@example.com", "password": "admin-password"}
DEMO_FLOW_PATH = Path(__file__).resolve().parents[3] / "flows" / "hvylia-demo.json"


@pytest.fixture
async def settings(database_url: SecretStr) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
        demo_flow_path=DEMO_FLOW_PATH,
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
    await engine.dispose()
    return settings


def _start(settings: Settings, server: FakeServer) -> TestClient:
    return TestClient(
        create_app(
            settings, create_engine(settings), FakeAsyncRedis(server=server, decode_responses=True)
        )
    )


async def _versions(settings: Settings) -> list[tuple[int, str | None]]:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db:
        rows = await db.execute(
            select(BotVersion.version, BotVersion.note)
            .where(BotVersion.bot_id == settings.demo_bot_id)
            .order_by(BotVersion.version)
        )
        versions = [(version, note) for version, note in rows]
    await engine.dispose()
    return versions


@pytest.mark.integration
async def test_first_start_creates_and_publishes_the_demo_bot(settings: Settings) -> None:
    server = FakeServer()
    with _start(settings, server) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        bot = http.get(f"/bots/{settings.demo_bot_id}").json()
        draft = http.get(f"/bots/{settings.demo_bot_id}/draft").json()

    assert (bot["name"], bot["provider"], bot["model"]) == (
        "Hvylia Mobile demo",
        "openai",
        "gpt-4.1-mini",
    )
    assert bot["live_version"] == 1
    assert draft["updated_at"] is not None
    expected = FlowGraph.model_validate(json.loads(DEMO_FLOW_PATH.read_text(encoding="utf-8")))
    assert draft["hash"] == expected.logic_hash()
    assert await _versions(settings) == [(1, "seeded demo flow")]

    redis = FakeAsyncRedis(server=server, decode_responses=True)
    live = await live_bots.read(redis, settings.demo_bot_id)
    assert live is not None
    assert live.version == 1
    assert FlowGraph.model_validate(live.flow).same_logic(expected)


@pytest.mark.integration
async def test_restart_keeps_the_bot_and_its_edits(settings: Settings) -> None:
    server = FakeServer()
    with _start(settings, server) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        url = f"/bots/{settings.demo_bot_id}/draft"
        edited = http.get(url).json()["flow"]
        edited["nodes"][0]["data"]["greeting"] = "Змінене привітання"
        http.put(url, json=edited)

    with _start(settings, server) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        draft = http.get(f"/bots/{settings.demo_bot_id}/draft").json()["flow"]

    assert draft["nodes"][0]["data"]["greeting"] == "Змінене привітання"
    assert await _versions(settings) == [(1, "seeded demo flow")]


@pytest.mark.integration
def test_seed_can_be_turned_off(settings: Settings) -> None:
    with _start(settings.model_copy(update={"demo_seed": False}), FakeServer()) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        assert http.get("/bots").json() == []


@pytest.mark.integration
def test_broken_flow_file_fails_the_start(settings: Settings, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="is invalid"),
        _start(settings.model_copy(update={"demo_flow_path": broken}), FakeServer()),
    ):
        pass


# --- republishing a changed flow file (ROZ-82) -------------------------------------------


def _changed_flow(tmp_path: Path, greeting: str = "Нове привітання з файлу") -> Path:
    data = json.loads(DEMO_FLOW_PATH.read_text(encoding="utf-8"))
    data["nodes"][0]["data"]["greeting"] = greeting
    path = tmp_path / "hvylia-demo.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _greeting(http: TestClient, settings: Settings) -> str:
    http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
    draft = http.get(f"/bots/{settings.demo_bot_id}/draft").json()["flow"]
    greeting: str = draft["nodes"][0]["data"]["greeting"]
    return greeting


@pytest.mark.integration
async def test_changed_flow_file_is_published_as_a_new_version(
    settings: Settings, tmp_path: Path
) -> None:
    server = FakeServer()
    with _start(settings, server):
        pass
    changed = settings.model_copy(update={"demo_flow_path": _changed_flow(tmp_path)})

    with _start(changed, server) as http:
        greeting = _greeting(http, changed)
    with _start(changed, server):  # a restart with the same file publishes nothing
        pass

    assert greeting == "Нове привітання з файлу"
    assert await _versions(settings) == [(1, "seeded demo flow"), (2, REPUBLISHED_NOTE)]
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    live = await live_bots.read(redis, settings.demo_bot_id)
    assert live is not None
    assert live.version == 2
    assert live.flow["nodes"][0]["data"]["greeting"] == "Нове привітання з файлу"


@pytest.mark.integration
async def test_a_version_published_by_a_person_is_not_overwritten(
    settings: Settings, tmp_path: Path
) -> None:
    server = FakeServer()
    with _start(settings, server) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        url = f"/bots/{settings.demo_bot_id}"
        edited = http.get(f"{url}/draft").json()["flow"]
        edited["nodes"][0]["data"]["greeting"] = "Привітання з адмінки"
        http.put(f"{url}/draft", json=edited)
        assert http.post(f"{url}/publish", json={"note": "admin edit"}).status_code == 201
    changed = settings.model_copy(update={"demo_flow_path": _changed_flow(tmp_path)})

    with _start(changed, server) as http:
        greeting = _greeting(http, changed)

    assert greeting == "Привітання з адмінки"
    assert await _versions(settings) == [(1, "seeded demo flow"), (2, "admin edit")]


@pytest.mark.integration
async def test_unpublished_draft_edits_are_not_overwritten(
    settings: Settings, tmp_path: Path
) -> None:
    server = FakeServer()
    with _start(settings, server) as http:
        http.headers[CSRF_HEADER] = http.post("/auth/login", json=ADMIN).json()["csrf_token"]
        url = f"/bots/{settings.demo_bot_id}/draft"
        edited = http.get(url).json()["flow"]
        edited["nodes"][0]["data"]["greeting"] = "Чернетка з адмінки"
        http.put(url, json=edited)
    changed = settings.model_copy(update={"demo_flow_path": _changed_flow(tmp_path)})

    with _start(changed, server) as http:
        greeting = _greeting(http, changed)

    assert greeting == "Чернетка з адмінки"
    assert await _versions(settings) == [(1, "seeded demo flow")]


@pytest.mark.integration
async def test_replicas_starting_together_publish_one_version(
    settings: Settings, tmp_path: Path
) -> None:
    server = FakeServer()
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    engines = [create_engine(settings) for _ in range(2)]
    factories = [create_session_factory(engine) for engine in engines]

    # Both create the bot from scratch, then both republish a changed file.
    await asyncio.gather(*(seed_demo_bot(settings, f, redis) for f in factories))
    changed = settings.model_copy(update={"demo_flow_path": _changed_flow(tmp_path)})
    await asyncio.gather(*(seed_demo_bot(changed, f, redis) for f in factories))
    for engine in engines:
        await engine.dispose()

    assert await _versions(settings) == [(1, "seeded demo flow"), (2, REPUBLISHED_NOTE)]
