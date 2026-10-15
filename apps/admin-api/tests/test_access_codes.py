"""Demo access codes: code format and session index without Docker, the full issue → login →
revoke flow against a migrated PostgreSQL."""

import re
from collections.abc import Iterator

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete

from admin_api.access_codes import ALPHABET, code_hash, generate_code, normalize_code
from admin_api.app import create_app
from admin_api.auth import require_admin
from admin_api.sessions import CSRF_HEADER, SessionStore
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import AccessCode, User
from rozmova_core.settings import Settings

# --- No Docker -------------------------------------------------------------------


def test_generated_codes_are_grouped_and_unambiguous() -> None:
    codes = {generate_code() for _ in range(200)}

    assert len(codes) == 200
    for code in codes:
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
        assert set(code.replace("-", "")) <= set(ALPHABET)
    assert not set("01OIL") & set(ALPHABET)


def test_typed_variants_of_a_code_hash_the_same() -> None:
    assert normalize_code(" k7qm-2xrp 9htw ") == "K7QM2XRP9HTW"
    assert code_hash("k7qm 2xrp 9htw") == code_hash("K7QM-2XRP-9HTW")
    assert code_hash("K7QM-2XRP-9HTW") != code_hash("K7QM-2XRP-9HTX")
    assert "K7QM" not in code_hash("K7QM-2XRP-9HTW")


async def test_ending_code_sessions_leaves_other_sessions() -> None:
    store = SessionStore(FakeAsyncRedis(decode_responses=True), ttl_seconds=600)
    a1, _ = await store.create(name="Acme", role="viewer", access_code_id="code-a")
    a2, _ = await store.create(name="Acme", role="viewer", access_code_id="code-a")
    b, _ = await store.create(name="Beta", role="viewer", access_code_id="code-b")
    admin, _ = await store.create(name="admin@example.com", role="admin", user_id="u1")

    assert await store.end_access_code_sessions("code-a") == 2
    assert await store.end_access_code_sessions("code-a") == 0

    assert await store.get(a1) is None
    assert await store.get(a2) is None
    assert await store.get(b) is not None
    assert await store.get(admin) is not None


async def test_session_has_exactly_one_subject() -> None:
    store = SessionStore(FakeAsyncRedis(decode_responses=True), ttl_seconds=600)
    with pytest.raises(ValueError, match="exactly one"):
        await store.create(name="x", role="viewer")
    with pytest.raises(ValueError, match="exactly one"):
        await store.create(name="x", role="viewer", user_id="u", access_code_id="c")


# --- PostgreSQL (testcontainers) --------------------------------------------------

ADMIN = {"email": "admin@example.com", "password": "admin-password"}


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
        await db.execute(delete(AccessCode))
        await db.execute(delete(User))
    await engine.dispose()
    return settings


class Browsers:
    """Two browsers on one admin API: the admin's and a demo viewer's. Each has its own
    app instance (and event loop); they share the database and one fake Redis server."""

    def __init__(self, settings: Settings) -> None:
        server = FakeServer()
        self.admin = self._client(settings, server)
        self.viewer = self._client(settings, server)

    @staticmethod
    def _client(settings: Settings, server: FakeServer) -> TestClient:
        app = create_app(
            settings,
            create_engine(settings),
            FakeAsyncRedis(server=server, decode_responses=True),
        )

        @app.put("/test/draft", dependencies=[Depends(require_admin)])
        async def save_draft() -> None:
            return None

        return TestClient(app)


@pytest.fixture
def browsers(settings: Settings) -> Iterator[Browsers]:
    browsers = Browsers(settings)
    with browsers.admin, browsers.viewer:
        yield browsers


def _admin_csrf(http: TestClient) -> dict[str, str]:
    body = http.post("/auth/login", json=ADMIN).json()
    return {CSRF_HEADER: body["csrf_token"]}


@pytest.mark.integration
def test_issue_login_revoke(browsers: Browsers) -> None:
    admin = _admin_csrf(browsers.admin)

    issued = browsers.admin.post("/access-codes", json={"label": " Acme "}, headers=admin)
    assert issued.status_code == 201
    code = issued.json()["code"]
    assert issued.json()["label"] == "Acme"
    assert issued.json()["last_used_at"] is None

    # The viewer types the code in lower case with spaces.
    login = browsers.viewer.post("/auth/access-code", json={"code": code.lower().replace("-", " ")})
    assert login.status_code == 200
    viewer = login.json()
    assert viewer["user"] == {"id": issued.json()["id"], "name": "Acme", "role": "viewer"}
    assert "HttpOnly" in login.headers["set-cookie"]

    # Viewers look but do not change anything, and do not see other companies' codes.
    me = browsers.viewer.get("/auth/me")
    assert me.json()["user"]["role"] == "viewer"
    csrf = {CSRF_HEADER: viewer["csrf_token"]}
    assert browsers.viewer.put("/test/draft", headers=csrf).status_code == 403
    assert browsers.viewer.get("/access-codes").status_code == 403

    [listed] = browsers.admin.get("/access-codes").json()
    assert listed["last_used_at"] is not None
    assert "code" not in listed

    revoke = browsers.admin.delete(f"/access-codes/{listed['id']}", headers=admin)
    assert revoke.status_code == 204

    # The live viewer session ended with the code, and the code no longer works.
    assert browsers.viewer.get("/auth/me").status_code == 401
    again = browsers.viewer.post("/auth/access-code", json={"code": code})
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "invalid_access_code"
    [revoked] = browsers.admin.get("/access-codes").json()
    assert revoked["revoked_at"] is not None


@pytest.mark.integration
def test_unknown_code_and_admin_only_routes(browsers: Browsers) -> None:
    unknown = browsers.viewer.post("/auth/access-code", json={"code": "AAAA-BBBB-CCCC"})
    assert unknown.status_code == 401
    assert "set-cookie" not in unknown.headers

    assert browsers.admin.get("/access-codes").status_code == 401  # not logged in yet
    admin = _admin_csrf(browsers.admin)
    assert browsers.admin.post("/access-codes", json={"label": "x"}).status_code == 403  # CSRF
    blank = browsers.admin.post("/access-codes", json={"label": "  "}, headers=admin)
    assert blank.status_code == 422

    missing = browsers.admin.delete(
        "/access-codes/00000000-0000-0000-0000-000000000000", headers=admin
    )
    assert missing.status_code == 404


@pytest.mark.integration
def test_revoke_is_idempotent(browsers: Browsers) -> None:
    admin = _admin_csrf(browsers.admin)
    code_id = browsers.admin.post("/access-codes", json={"label": "Acme"}, headers=admin).json()[
        "id"
    ]

    assert browsers.admin.delete(f"/access-codes/{code_id}", headers=admin).status_code == 204
    first = browsers.admin.get("/access-codes").json()[0]["revoked_at"]
    assert browsers.admin.delete(f"/access-codes/{code_id}", headers=admin).status_code == 204
    assert browsers.admin.get("/access-codes").json()[0]["revoked_at"] == first
