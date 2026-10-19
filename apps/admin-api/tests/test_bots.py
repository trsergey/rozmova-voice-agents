"""Bots, the in-place draft and validation against a migrated PostgreSQL."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete

from admin_api.app import create_app
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, User
from rozmova_core.settings import Settings
from rozmova_engine.evals.report import EvalReport, Summary

DEMO_FLOW: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "flows/hvylia-demo.json").read_text(encoding="utf-8")
)
ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}
BOT = {"name": "Kyivstar support", "provider": "openai", "model": "gpt-4.1-mini"}


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
    """A logged-in browser: the session cookie plus the CSRF header on every request."""

    def __init__(self, settings: Settings, server: FakeServer) -> None:
        app = create_app(
            settings, create_engine(settings), FakeAsyncRedis(server=server, decode_responses=True)
        )
        self.http = TestClient(app)

    def login(self, credentials: dict[str, str]) -> None:
        body = self.http.post("/auth/login", json=credentials).json()
        self.http.headers[CSRF_HEADER] = body["csrf_token"]


@pytest.fixture
def clients(settings: Settings) -> Iterator[tuple[Client, Client]]:
    server = FakeServer()
    admin, viewer = Client(settings, server), Client(settings, server)
    with admin.http, viewer.http:
        admin.login(ADMIN)
        viewer.login(VIEWER)
        yield admin, viewer


def _create_bot(admin: Client) -> dict[str, Any]:
    response = admin.http.post("/bots", json=BOT)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


@pytest.mark.integration
def test_create_list_get(clients: tuple[Client, Client]) -> None:
    admin, viewer = clients

    bot = _create_bot(admin)

    assert {k: bot[k] for k in BOT} == BOT
    assert bot["live_version_id"] is None
    assert bot["draft_updated_at"] is None
    assert [b["id"] for b in viewer.http.get("/bots").json()] == [bot["id"]]
    assert viewer.http.get(f"/bots/{bot['id']}").json() == bot | {"last_eval": None}

    missing = viewer.http.get("/bots/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "bot_not_found"


@pytest.mark.integration
def test_bad_bot_is_422(clients: tuple[Client, Client]) -> None:
    admin, _ = clients

    assert admin.http.post("/bots", json={**BOT, "provider": "nope"}).status_code == 422
    assert admin.http.post("/bots", json={**BOT, "name": "  "}).status_code == 422


@pytest.mark.integration
def test_new_bot_has_an_empty_draft(clients: tuple[Client, Client]) -> None:
    admin, _ = clients
    bot = _create_bot(admin)

    draft = admin.http.get(f"/bots/{bot['id']}/draft").json()

    assert draft["flow"] == {"nodes": [], "edges": []}
    assert draft["updated_at"] is None
    assert len(draft["hash"]) == 64


@pytest.mark.integration
def test_save_draft_in_place(clients: tuple[Client, Client]) -> None:
    admin, viewer = clients
    bot = _create_bot(admin)
    url = f"/bots/{bot['id']}/draft"

    saved = admin.http.put(url, json=DEMO_FLOW)

    assert saved.status_code == 200, saved.text
    read = viewer.http.get(url).json()
    assert read == saved.json()
    assert read["updated_at"] is not None
    assert viewer.http.get(f"/bots/{bot['id']}").json()["draft_updated_at"] == read["updated_at"]

    # Moving nodes on the canvas and reordering keeps the hash; a logic change does not.
    moved = {
        "nodes": [{**n, "ui": {"x": 1, "y": 2}} for n in DEMO_FLOW["nodes"]][::-1],
        "edges": DEMO_FLOW["edges"][::-1],
    }
    assert admin.http.put(url, json=moved).json()["hash"] == read["hash"]
    rewired = {**DEMO_FLOW, "edges": DEMO_FLOW["edges"][1:]}
    assert admin.http.put(url, json=rewired).json()["hash"] != read["hash"]


@pytest.mark.integration
def test_draft_shape_is_checked_but_logic_is_not(clients: tuple[Client, Client]) -> None:
    admin, _ = clients
    bot = _create_bot(admin)
    url = f"/bots/{bot['id']}/draft"

    # Unknown node type: the contract rejects it.
    bad = {"nodes": [{"id": "x", "type": "teleport"}], "edges": []}
    assert admin.http.put(url, json=bad).status_code == 422
    # A dangling edge is a logic problem: the draft still saves, validation reports it.
    extra_edge = {"source": "start", "target": "nowhere"}
    dangling = {**DEMO_FLOW, "edges": [*DEMO_FLOW["edges"], extra_edge]}
    assert admin.http.put(url, json=dangling).status_code == 200


@pytest.mark.integration
def test_validate_reports_node_ids(clients: tuple[Client, Client]) -> None:
    admin, viewer = clients
    bot = _create_bot(admin)
    url = f"/bots/{bot['id']}"

    admin.http.put(f"{url}/draft", json=DEMO_FLOW)
    assert viewer.http.post(f"{url}/validate").json()["errors"] == []

    [route_edge] = [e for e in DEMO_FLOW["edges"] if e.get("when")][:1]
    broken = {**DEMO_FLOW, "edges": [e for e in DEMO_FLOW["edges"] if e is not route_edge]}
    admin.http.put(f"{url}/draft", json=broken)
    result = viewer.http.post(f"{url}/validate").json()

    assert set(result) == {"errors", "warnings"}
    assert {"node_id": route_edge["source"], "code": "missing_route_edge"} in [
        {"node_id": e["node_id"], "code": e["code"]} for e in result["errors"]
    ]

    empty = admin.http.post(f"/bots/{_create_bot(admin)['id']}/validate").json()
    assert [(e["node_id"], e["code"]) for e in empty["errors"]] == [(None, "no_start")]


@pytest.mark.integration
def test_viewer_cannot_create_or_save(clients: tuple[Client, Client]) -> None:
    admin, viewer = clients
    bot = _create_bot(admin)

    for response in (
        viewer.http.post("/bots", json=BOT),
        viewer.http.put(f"/bots/{bot['id']}/draft", json=DEMO_FLOW),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.integration
def test_bots_need_a_session(settings: Settings) -> None:
    with Client(settings, FakeServer()).http as http:
        assert http.get("/bots").status_code == 401


def _report(passed: bool, cases_passed: int) -> dict[str, Any]:
    started = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    report = EvalReport(
        started_at=started,
        finished_at=started + timedelta(minutes=3),
        flow="draft",
        provider="openai",
        model="gpt-4.1-mini",
        judge_model=None,
        subset="full",
        summary=Summary(
            passed=passed,
            cases=50,
            cases_passed=cases_passed,
            gates=[],
            by_tag={},
            input_tokens=0,
            output_tokens=0,
            judge_cost_usd=None,
            judge_errors=0,
        ),
        cases=[],
    )
    return report.model_dump(mode="json")


async def _store_report(settings: Settings, bot_id: str, stored: dict[str, Any] | None) -> None:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        bot = await db.get(Bot, UUID(bot_id))
        assert bot is not None
        bot.draft_eval_report = stored
    await engine.dispose()


@pytest.mark.integration
def test_bot_card_shows_the_last_eval(clients: tuple[Client, Client], settings: Settings) -> None:
    admin, viewer = clients
    bot = _create_bot(admin)
    url = f"/bots/{bot['id']}"
    draft_hash = admin.http.put(f"{url}/draft", json=DEMO_FLOW).json()["hash"]

    stored = {"draft_hash": draft_hash, "report": _report(passed=True, cases_passed=50)}
    asyncio.run(_store_report(settings, bot["id"], stored))
    last_eval = viewer.http.get(url).json()["last_eval"]
    assert last_eval == {
        "passed": True,
        "cases": 50,
        "cases_passed": 50,
        "finished_at": "2026-10-19T12:03:00Z",
        "current": True,
    }

    changed = {**DEMO_FLOW, "edges": DEMO_FLOW["edges"][:-1]}
    admin.http.put(f"{url}/draft", json=changed)
    assert viewer.http.get(url).json()["last_eval"]["current"] is False

    asyncio.run(_store_report(settings, bot["id"], {"draft_hash": "x", "report": {"bad": 1}}))
    assert viewer.http.get(url).json()["last_eval"] is None


@pytest.mark.integration
def test_providers_come_from_the_engine(clients: tuple[Client, Client]) -> None:
    _, viewer = clients

    expected = ["anthropic", "azure_openai", "openai", "openrouter"]
    assert viewer.http.get("/providers").json() == expected
