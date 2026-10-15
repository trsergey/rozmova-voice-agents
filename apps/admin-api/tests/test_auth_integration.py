"""Login, logout and the admin seed against a migrated PostgreSQL (testcontainers)."""

import asyncio

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from admin_api.app import create_app
from admin_api.passwords import hash_password
from admin_api.sessions import COOKIE_NAME, CSRF_HEADER
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import User
from rozmova_core.settings import Settings

EMAIL = "admin@example.com"
PASSWORD = "first-password"  # noqa: S105 - a test-only password


@pytest.fixture
async def settings(database_url: SecretStr) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        admin_email=f"  {EMAIL.upper()} ",  # normalized by the seed
        admin_password=SecretStr(PASSWORD),
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(User))
    await engine.dispose()
    return settings


def _client(settings: Settings) -> TestClient:
    return TestClient(
        create_app(settings, create_engine(settings), FakeAsyncRedis(decode_responses=True))
    )


def _login(http: TestClient, email: str, password: str) -> int:
    return http.post("/auth/login", json={"email": email, "password": password}).status_code


@pytest.mark.integration
def test_login_session_and_logout(settings: Settings) -> None:
    with _client(settings) as http:
        response = http.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["name"] == EMAIL
        assert body["user"]["role"] == "admin"
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Secure" not in cookie  # env=test; deployed environments set it

        assert http.get("/auth/me").json() == body
        assert http.post("/auth/logout").status_code == 403  # no CSRF token
        logout = http.post("/auth/logout", headers={CSRF_HEADER: body["csrf_token"]})
        assert logout.status_code == 204
        assert http.get("/auth/me").status_code == 401


@pytest.mark.integration
def test_wrong_password_and_unknown_email_look_the_same(settings: Settings) -> None:
    with _client(settings) as http:
        wrong = http.post("/auth/login", json={"email": EMAIL, "password": "nope"})
        unknown = http.post("/auth/login", json={"email": "x@example.com", "password": "nope"})

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()
        assert wrong.json()["error"]["code"] == "invalid_credentials"
        assert "set-cookie" not in wrong.headers


@pytest.mark.integration
def test_secure_cookie_outside_local_and_test(settings: Settings) -> None:
    with _client(settings.model_copy(update={"env": "dev"})) as http:
        response = http.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

        assert "Secure" in response.headers["set-cookie"]


@pytest.mark.integration
def test_seed_creates_then_resets_password(settings: Settings) -> None:
    with _client(settings) as http:
        assert _login(http, EMAIL, PASSWORD) == 200

    changed = settings.model_copy(update={"admin_password": SecretStr("second-password")})
    with _client(changed) as http:
        assert _login(http, EMAIL, PASSWORD) == 401
        assert _login(http, EMAIL, "second-password") == 200


@pytest.mark.integration
async def test_seed_promotes_existing_viewer_and_keeps_one_row(settings: Settings) -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as db, db.begin():
        db.add(User(email=EMAIL, password_hash=hash_password("old"), role="viewer"))

    def start_twice() -> None:
        for _ in range(2):
            with _client(settings) as http:
                assert (
                    http.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()[
                        "user"
                    ]["role"]
                    == "admin"
                )

    await asyncio.to_thread(start_twice)

    async with factory() as db:
        users = (await db.scalars(select(User))).all()
    await engine.dispose()
    assert [(u.email, u.role) for u in users] == [(EMAIL, "admin")]


@pytest.mark.integration
async def test_viewer_user_cannot_mutate(settings: Settings) -> None:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        db.add(User(email="viewer@example.com", password_hash=hash_password("v"), role="viewer"))
    await engine.dispose()

    def check() -> None:
        from fastapi import Depends

        from admin_api.auth import require_admin

        app = create_app(settings, create_engine(settings), FakeAsyncRedis(decode_responses=True))
        app.post("/test/publish", dependencies=[Depends(require_admin)])(lambda: None)
        with TestClient(app) as http:
            body = http.post(
                "/auth/login", json={"email": "viewer@example.com", "password": "v"}
            ).json()
            assert body["user"]["role"] == "viewer"
            response = http.post("/test/publish", headers={CSRF_HEADER: body["csrf_token"]})
            assert response.status_code == 403

    await asyncio.to_thread(check)
