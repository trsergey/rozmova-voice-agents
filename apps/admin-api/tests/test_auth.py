"""Passwords, the session store, CSRF and roles without Docker (fakeredis, no database)."""

from collections.abc import Iterator

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr

from admin_api.app import create_app
from admin_api.auth import require_admin
from admin_api.passwords import hash_password, verify_password
from admin_api.sessions import COOKIE_NAME, CSRF_HEADER, Role, SessionStore
from rozmova_core.db import create_engine
from rozmova_core.settings import Settings

SETTINGS = Settings(
    env="test", database_url=SecretStr("postgresql+asyncpg://u:p@nowhere:5432/none")
)

# --- Passwords -----------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    stored = hash_password("correct horse")

    assert stored.startswith("scrypt$32768$8$1$")
    assert "correct horse" not in stored
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert hash_password("correct horse") != stored  # a fresh salt every time


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AA==$AA=="])
def test_malformed_hash_is_a_mismatch(stored: str) -> None:
    assert not verify_password("anything", stored)


# --- Session store -------------------------------------------------------------


async def test_session_store_create_get_delete() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    store = SessionStore(redis, ttl_seconds=600)

    session_id, session = await store.create(name="a@example.com", role="admin", user_id="u1")

    assert await store.get(session_id) == session
    assert await store.get("forged") is None
    # Redis holds a digest of the cookie value, never the value itself.
    [key] = [str(k) for k in await redis.keys("*")]
    assert key.startswith("admin-session:")
    assert session_id not in key

    await redis.expire(key, 5)
    await store.get(session_id)
    assert await redis.ttl(key) > 500  # reading extends the session

    await store.delete(session_id)
    assert await store.get(session_id) is None


# --- CSRF and roles through the app ---------------------------------------------


class AdminApi:
    """The app on a fake Redis plus a way to log in without a database.

    The app and the test run on different event loops, so each side gets its own fakeredis
    client on one shared server.
    """

    def __init__(self) -> None:
        self.server = FakeServer()
        app = create_app(
            SETTINGS,
            create_engine(SETTINGS),  # never connects: no admin to seed, no login
            FakeAsyncRedis(server=self.server, decode_responses=True),
        )

        @app.post("/test/edit", dependencies=[Depends(require_admin)])
        async def edit() -> dict[str, bool]:
            return {"ok": True}

        self.http = TestClient(app)

    async def login_as(self, role: Role) -> str:
        """Start a session as ``role``; sets the cookie and returns the CSRF token."""
        store = SessionStore(FakeAsyncRedis(server=self.server, decode_responses=True), 600)
        session_id, session = await store.create(
            name=f"{role}@example.com", role=role, user_id="u1"
        )
        self.http.cookies.set(COOKIE_NAME, session_id)
        return session.csrf_token


@pytest.fixture
def api() -> Iterator[AdminApi]:
    api = AdminApi()
    with api.http:
        yield api


def test_healthz(api: AdminApi) -> None:
    assert api.http.get("/healthz").json() == {"status": "ok"}


def test_without_session_is_401(api: AdminApi) -> None:
    for response in (api.http.get("/auth/me"), api.http.post("/test/edit")):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    api.http.cookies.set(COOKIE_NAME, "forged")
    assert api.http.get("/auth/me").status_code == 401


async def test_me_returns_role_and_csrf_token(api: AdminApi) -> None:
    csrf = await api.login_as("viewer")

    body = api.http.get("/auth/me").json()

    assert body == {
        "user": {"id": "u1", "name": "viewer@example.com", "role": "viewer"},
        "csrf_token": csrf,
    }


async def test_mutating_request_needs_csrf_token(api: AdminApi) -> None:
    csrf = await api.login_as("admin")

    missing = api.http.post("/test/edit")
    wrong = api.http.post("/test/edit", headers={CSRF_HEADER: "nope"})
    right = api.http.post("/test/edit", headers={CSRF_HEADER: csrf})

    assert (missing.status_code, wrong.status_code) == (403, 403)
    assert missing.json()["error"]["code"] == "csrf_failed"
    assert right.json() == {"ok": True}


async def test_viewer_gets_403_on_admin_routes(api: AdminApi) -> None:
    csrf = await api.login_as("viewer")

    response = api.http.post("/test/edit", headers={CSRF_HEADER: csrf})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_logout_needs_csrf_and_ends_the_session(api: AdminApi) -> None:
    csrf = await api.login_as("admin")
    session_id = api.http.cookies[COOKIE_NAME]

    assert api.http.post("/auth/logout").status_code == 403
    response = api.http.post("/auth/logout", headers={CSRF_HEADER: csrf})

    assert response.status_code == 204
    cleared = response.headers["set-cookie"]
    assert cleared.startswith(f'{COOKIE_NAME}=""')
    assert "Max-Age=0" in cleared
    # A copy of the old cookie no longer works either: the session is gone server-side.
    api.http.cookies.set(COOKIE_NAME, session_id)
    assert api.http.get("/auth/me").status_code == 401
