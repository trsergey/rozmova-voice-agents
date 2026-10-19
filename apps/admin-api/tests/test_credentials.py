"""Provider keys: encrypted at rest, write-only, ``last4`` in responses (FR-17)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from admin_api.app import create_app
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_core import live_bots
from rozmova_core.credentials import CredentialCipher
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import Bot, ProviderCredential, User
from rozmova_core.settings import Settings

ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}
BOT = {"name": "Kyivstar support", "provider": "openai", "model": "gpt-4.1-mini"}
KEY = "sk-proj-very-secret-key-a1b2"
MASTER_KEY = SecretStr(Fernet.generate_key().decode())


@pytest.fixture
async def settings(database_url: SecretStr) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
        fernet_master_key=MASTER_KEY,
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(Bot))
        await db.execute(delete(User))
        viewer_hash = hash_password(VIEWER["password"])
        db.add(User(email=VIEWER["email"], password_hash=viewer_hash, role="viewer"))
    await engine.dispose()
    return settings


def _client(settings: Settings, server: FakeServer, credentials: dict[str, str]) -> TestClient:
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    http = TestClient(create_app(settings, create_engine(settings), redis))
    http.__enter__()
    body = http.post("/auth/login", json=credentials).json()
    http.headers[CSRF_HEADER] = body["csrf_token"]
    return http


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def clients(settings: Settings, server: FakeServer) -> Iterator[tuple[TestClient, TestClient]]:
    admin, viewer = _client(settings, server, ADMIN), _client(settings, server, VIEWER)
    try:
        yield admin, viewer
    finally:
        admin.__exit__(None, None, None)
        viewer.__exit__(None, None, None)


def _bot(admin: TestClient) -> str:
    response = admin.post("/bots", json=BOT)
    assert response.status_code == 201, response.text
    bot_id: str = response.json()["id"]
    return bot_id


async def _stored(settings: Settings) -> list[ProviderCredential]:
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db:
        rows = list(await db.scalars(select(ProviderCredential)))
    await engine.dispose()
    return rows


@pytest.mark.integration
def test_put_stores_ciphertext_and_returns_last4(
    clients: tuple[TestClient, TestClient], settings: Settings
) -> None:
    admin, viewer = clients
    bot_id = _bot(admin)

    response = admin.put(f"/bots/{bot_id}/credentials", json={"provider": "openai", "api_key": KEY})

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert (body["provider"], body["last4"]) == ("openai", "a1b2")
    assert KEY not in response.text
    listed = viewer.get(f"/bots/{bot_id}/credentials").json()
    assert listed == [body]

    [row] = asyncio.run(_stored(settings))
    assert KEY not in row.secret_ciphertext
    cipher = CredentialCipher(MASTER_KEY)
    assert cipher.decrypt(row.secret_ciphertext).get_secret_value() == KEY


@pytest.mark.integration
def test_second_put_replaces_the_key(
    clients: tuple[TestClient, TestClient], settings: Settings
) -> None:
    admin, _ = clients
    bot_id = _bot(admin)
    url = f"/bots/{bot_id}/credentials"

    admin.put(url, json={"provider": "openai", "api_key": KEY})
    admin.put(url, json={"provider": "openai", "api_key": "sk-proj-another-key-zz99"})

    assert [c["last4"] for c in admin.get(url).json()] == ["zz99"]
    assert len(asyncio.run(_stored(settings))) == 1


@pytest.mark.integration
async def test_put_tells_gateways_to_reload(settings: Settings, server: FakeServer) -> None:
    redis = FakeAsyncRedis(server=server, decode_responses=True)
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(live_bots.PUBLISHED_CHANNEL)
        await pubsub.get_message(timeout=1)  # the subscribe confirmation

        def put() -> str:
            admin = _client(settings, server, ADMIN)
            try:
                bot_id = _bot(admin)
                body = {"provider": "openai", "api_key": KEY}
                admin.put(f"/bots/{bot_id}/credentials", json=body)
                return bot_id
            finally:
                admin.__exit__(None, None, None)

        bot_id = await asyncio.to_thread(put)
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)

    assert message is not None
    assert message["data"] == bot_id


@pytest.mark.integration
def test_viewer_cannot_put(clients: tuple[TestClient, TestClient]) -> None:
    admin, viewer = clients
    bot_id = _bot(admin)

    body = {"provider": "openai", "api_key": KEY}
    response = viewer.put(f"/bots/{bot_id}/credentials", json=body)

    assert response.status_code == 403


@pytest.mark.integration
def test_unknown_bot_or_provider(clients: tuple[TestClient, TestClient]) -> None:
    admin, _ = clients
    body = {"provider": "openai", "api_key": KEY}

    missing = admin.put("/bots/00000000-0000-0000-0000-000000000000/credentials", json=body)
    assert missing.status_code == 404
    bad = admin.put(f"/bots/{_bot(admin)}/credentials", json={**body, "provider": "nope"})
    assert bad.status_code == 422


@pytest.mark.integration
def test_rejected_key_is_not_echoed(clients: tuple[TestClient, TestClient]) -> None:
    admin, _ = clients
    bot_id = _bot(admin)

    short_key, bad_provider = "sk-ab12", "nope"
    for body in (
        {"provider": "openai", "api_key": short_key},
        {"provider": bad_provider, "api_key": KEY},
    ):
        response = admin.put(f"/bots/{bot_id}/credentials", json=body)
        assert response.status_code == 422
        assert body["api_key"] not in response.text
        assert response.json()["detail"][0]["loc"][0] == "body"


@pytest.mark.integration
def test_without_master_key_keys_cannot_be_stored(settings: Settings, server: FakeServer) -> None:
    no_key = settings.model_copy(update={"fernet_master_key": None})
    admin = _client(no_key, server, ADMIN)
    try:
        response = admin.put(
            f"/bots/{_bot(admin)}/credentials", json={"provider": "openai", "api_key": KEY}
        )
    finally:
        admin.__exit__(None, None, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "credentials_unavailable"
