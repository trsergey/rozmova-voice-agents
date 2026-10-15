"""``POST /calls/ticket`` against a migrated PostgreSQL (with the seeded demo bot) and a
fake Redis: the ticket is the one the gateway accepts."""

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr

from admin_api.app import create_app
from admin_api.calls import ws_url
from admin_api.sessions import COOKIE_NAME, CSRF_HEADER, Role, SessionStore
from rozmova_core import tickets
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot
from rozmova_core.settings import Settings

SECRET = "test-ticket-secret"  # noqa: S105 - a test-only HMAC key
BOT_ID = UUID("00000000-0000-0000-0000-00000000d3e0")  # the seeded, published demo bot
DEMO_FLOW_PATH = Path(__file__).resolve().parents[3] / "flows" / "hvylia-demo.json"

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(database_url: SecretStr) -> Settings:
    return Settings(
        env="test",
        database_url=database_url,
        ticket_secret=SecretStr(SECRET),
        gateway_public_url="https://gateway.example/",
        demo_bot_id=BOT_ID,
        demo_flow_path=DEMO_FLOW_PATH,
    )


class AdminApi:
    def __init__(self, settings: Settings) -> None:
        self.server = FakeServer()
        app = create_app(settings, create_engine(settings), self.redis())
        self.http = TestClient(app)

    def redis(self) -> FakeAsyncRedis:
        """A client for the caller's event loop on the app's in-memory Redis."""
        return FakeAsyncRedis(server=self.server, decode_responses=True)

    async def login_as(
        self, role: Role, *, user_id: str | None = None, access_code_id: str | None = None
    ) -> dict[str, str]:
        """Start a session; returns the CSRF header for mutating requests."""
        store = SessionStore(self.redis(), 600)
        session_id, session = await store.create(
            name="someone", role=role, user_id=user_id, access_code_id=access_code_id
        )
        self.http.cookies.set(COOKIE_NAME, session_id)
        return {CSRF_HEADER: session.csrf_token}

    def ticket(self, csrf: dict[str, str], **body: object) -> dict[str, str]:
        response = self.http.post(
            "/calls/ticket", json={"bot_id": str(BOT_ID), **body}, headers=csrf
        )
        assert response.status_code == 200, response.text
        result: dict[str, str] = response.json()
        return result


@pytest.fixture
def api(settings: Settings) -> Iterator[AdminApi]:
    api = AdminApi(settings)
    with api.http:
        yield api


async def test_viewer_ticket_is_valid_once_on_the_gateway(api: AdminApi) -> None:
    code_id = str(uuid4())
    csrf = await api.login_as("viewer", access_code_id=code_id)

    out = api.ticket(csrf)

    ticket = tickets.verify(out["ticket"], SECRET)
    assert ticket.bot_id == BOT_ID
    assert str(ticket.call_id) == out["call_id"]
    assert ticket.user_id == f"code:{code_id}"
    assert ticket.debug is False
    # Single use, exactly as the gateway checks it.
    redis = api.redis()
    assert await tickets.consume(redis, ticket.id)
    assert not await tickets.consume(redis, ticket.id)


async def test_ws_url_is_ready_to_open(api: AdminApi) -> None:
    csrf = await api.login_as("admin", user_id="u1")

    out = api.ticket(csrf)

    url = urlsplit(out["ws_url"])
    assert (url.scheme, url.netloc, url.path) == ("wss", "gateway.example", f"/ws/calls/{BOT_ID}")
    assert parse_qs(url.query) == {"call_id": [out["call_id"]], "ticket": [out["ticket"]]}


def test_ws_url_scheme_follows_the_gateway_url() -> None:
    call_id = UUID(int=1)
    assert ws_url("http://localhost:8001", BOT_ID, call_id, "t").startswith("ws://localhost:8001/")
    assert ws_url("https://g.example", BOT_ID, call_id, "t").startswith("wss://g.example/")


async def test_admin_gets_debug_and_user_subject(api: AdminApi) -> None:
    csrf = await api.login_as("admin", user_id="u1")

    ticket = tickets.verify(api.ticket(csrf, debug=True)["ticket"], SECRET)

    assert ticket.debug is True
    assert ticket.user_id == "user:u1"


async def test_viewer_cannot_ask_for_debug(api: AdminApi) -> None:
    csrf = await api.login_as("viewer", access_code_id="c1")

    response = api.http.post(
        "/calls/ticket", json={"bot_id": str(BOT_ID), "debug": True}, headers=csrf
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_reconnect_keeps_the_call_id(api: AdminApi) -> None:
    csrf = await api.login_as("viewer", access_code_id="c1")
    first = api.ticket(csrf)

    again = api.ticket(csrf, call_id=first["call_id"])

    assert again["call_id"] == first["call_id"]
    assert again["ticket"] != first["ticket"]


async def test_needs_session_and_csrf(api: AdminApi) -> None:
    body = {"bot_id": str(BOT_ID)}
    assert api.http.post("/calls/ticket", json=body).status_code == 401

    await api.login_as("viewer", access_code_id="c1")
    response = api.http.post("/calls/ticket", json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


async def test_without_ticket_secret_is_503(settings: Settings) -> None:
    api = AdminApi(settings.model_copy(update={"ticket_secret": None}))
    with api.http:
        csrf = await api.login_as("admin", user_id="u1")
        response = api.http.post("/calls/ticket", json={"bot_id": str(BOT_ID)}, headers=csrf)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tickets_unavailable"


async def test_unknown_bot_is_404(api: AdminApi) -> None:
    csrf = await api.login_as("admin", user_id="u1")

    response = api.http.post("/calls/ticket", json={"bot_id": str(uuid4())}, headers=csrf)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "bot_not_found"


async def test_unpublished_bot_only_for_debug(api: AdminApi, settings: Settings) -> None:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        bot = Bot(name="draft only", provider="openai", model="gpt-4.1-mini")
        db.add(bot)
        await db.flush()
        bot_id = str(bot.id)
    await engine.dispose()
    csrf = await api.login_as("admin", user_id="u1")

    plain = api.http.post("/calls/ticket", json={"bot_id": bot_id}, headers=csrf)
    debug = api.http.post("/calls/ticket", json={"bot_id": bot_id, "debug": True}, headers=csrf)

    assert plain.status_code == 409
    assert plain.json()["error"]["code"] == "bot_not_published"
    assert debug.status_code == 200
