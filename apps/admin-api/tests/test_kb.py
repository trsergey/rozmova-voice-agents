"""The knowledge base in admin-api: the background ingest, ``/kb/reindex`` and
``/kb/documents``. Unit tests replace the ingest; the integration test ingests a small KB
into a migrated PostgreSQL with fake embeddings."""

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from admin_api import kb
from admin_api.app import create_app
from admin_api.passwords import hash_password
from admin_api.sessions import CSRF_HEADER
from rozmova_core.db import create_engine, create_session_factory
from rozmova_core.models import KbDocument, User
from rozmova_core.settings import Settings
from rozmova_engine.rag.embeddings import FakeEmbedder
from rozmova_engine.rag.ingest import IngestReport


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"openai_api_key": SecretStr("sk-test"), "kb_path": Path("kb")}
    return Settings(**(values | overrides))


ENGINE: AsyncEngine = None  # type: ignore[assignment]  # never touched: ingest is replaced


@dataclass
class OfflineEmbedder(FakeEmbedder):
    """FakeEmbedder with the parts of OpenAIEmbedder the indexer touches."""

    usage_tokens: int = 0

    @classmethod
    def create(cls, *_: object) -> "OfflineEmbedder":
        return cls()

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb, "OpenAIEmbedder", OfflineEmbedder)


async def test_keyless_indexer_does_not_start() -> None:
    indexer = kb.KbIndexer(_settings(openai_api_key=None), ENGINE)

    indexer.start()

    assert not indexer.available
    assert not indexer.running
    assert indexer.status.state == "idle"


async def test_finished_ingest_reports_done(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ingest(*_: object) -> IngestReport:
        return IngestReport(documents_added=2, chunks_embedded=5)

    monkeypatch.setattr(kb, "ingest_directory", ingest)
    indexer = kb.KbIndexer(_settings(), ENGINE)

    indexer.start()
    running = indexer.status
    await asyncio.sleep(0.01)

    assert running.state == "running"
    assert indexer.status.state == "done"
    assert indexer.status.report == IngestReport(documents_added=2, chunks_embedded=5)
    assert indexer.status.started_at is not None
    assert indexer.status.finished_at is not None


async def test_second_start_while_running_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    release = asyncio.Event()

    async def ingest(*_: object) -> IngestReport:
        nonlocal calls
        calls += 1
        await release.wait()
        return IngestReport()

    monkeypatch.setattr(kb, "ingest_directory", ingest)
    indexer = kb.KbIndexer(_settings(), ENGINE)

    indexer.start()
    await asyncio.sleep(0)
    indexer.start()
    release.set()
    await asyncio.sleep(0.01)

    assert calls == 1
    assert indexer.status.state == "done"


async def test_aclose_cancels_a_running_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def hang(*_: object) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(kb, "ingest_directory", hang)
    indexer = kb.KbIndexer(_settings(), ENGINE)
    indexer.start()
    await started.wait()

    await indexer.aclose()

    assert not indexer.running


async def test_failed_ingest_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_: object) -> None:
        raise ConnectionError("OpenAI is down, key sk-secret")

    monkeypatch.setattr(kb, "ingest_directory", fail)
    indexer = kb.KbIndexer(_settings(), ENGINE)

    indexer.start()
    await asyncio.sleep(0.01)

    assert indexer.status.state == "failed"
    assert "sk-secret" not in indexer.status.model_dump_json()


async def test_aclose_without_a_task_is_a_no_op() -> None:
    await kb.KbIndexer(_settings(), ENGINE).aclose()


# --- endpoints against PostgreSQL -------------------------------------------------------

ADMIN = {"email": "admin@example.com", "password": "admin-password"}
VIEWER = {"email": "viewer@example.com", "password": "viewer-password"}


def _kb_dir(root: Path) -> Path:
    (root / "tariffs").mkdir()
    (root / "README.md").write_text("# Not knowledge\n", encoding="utf-8")
    (root / "roaming.md").write_text("# Роумінг\n\nРоумінг у Польщі.\n", encoding="utf-8")
    (root / "tariffs" / "basic.md").write_text(
        "# Тариф Базовий\n\n## Ціна\n\n150 грн на місяць.\n\n## Інтернет\n\n10 ГБ.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
async def settings(database_url: SecretStr, tmp_path: Path) -> Settings:
    settings = Settings(
        env="test",
        database_url=database_url,
        demo_seed=False,
        admin_email=ADMIN["email"],
        admin_password=SecretStr(ADMIN["password"]),
        openai_api_key=SecretStr("sk-test"),
        kb_path=_kb_dir(tmp_path),
    )
    engine = create_engine(settings)
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(delete(KbDocument))
        await db.execute(delete(User))
        viewer_hash = hash_password(VIEWER["password"])
        db.add(User(email=VIEWER["email"], password_hash=viewer_hash, role="viewer"))
    await engine.dispose()
    return settings


def _login(http: TestClient, credentials: dict[str, str]) -> None:
    body = http.post("/auth/login", json=credentials).json()
    http.headers[CSRF_HEADER] = body["csrf_token"]


@pytest.fixture
def clients(settings: Settings) -> Iterator[tuple[TestClient, TestClient]]:
    server = FakeServer()

    def client() -> TestClient:
        redis = FakeAsyncRedis(server=server, decode_responses=True)
        return TestClient(create_app(settings, create_engine(settings), redis))

    with client() as admin, client() as viewer:
        _login(admin, ADMIN)
        _login(viewer, VIEWER)
        yield admin, viewer


def _wait_done(http: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status: dict[str, Any] = http.get("/kb/reindex").json()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("reindex did not finish")


@pytest.mark.integration
def test_reindex_then_list_documents(clients: tuple[TestClient, TestClient]) -> None:
    admin, viewer = clients
    assert admin.get("/kb/reindex").json()["state"] == "idle"
    assert admin.get("/kb/documents").json() == []

    started = admin.post("/kb/reindex")

    assert started.status_code == 202, started.text
    assert started.json()["state"] in {"running", "done"}
    status = _wait_done(admin)
    assert status["state"] == "done"
    assert status["report"]["documents_added"] == 2

    documents = viewer.get("/kb/documents").json()
    assert [(d["source_path"], d["title"]) for d in documents] == [
        ("roaming.md", "Роумінг"),
        ("tariffs/basic.md", "Тариф Базовий"),
    ]
    assert all(d["chunks"] >= 1 for d in documents)


@pytest.mark.integration
def test_only_admins_reindex(clients: tuple[TestClient, TestClient]) -> None:
    _, viewer = clients

    response = viewer.post("/kb/reindex")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.integration
def test_reindex_without_a_key_is_unavailable(settings: Settings) -> None:
    keyless = settings.model_copy(update={"openai_api_key": None})
    redis = FakeAsyncRedis(decode_responses=True)
    with TestClient(create_app(keyless, create_engine(keyless), redis)) as admin:
        _login(admin, ADMIN)

        response = admin.post("/kb/reindex")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embeddings_unavailable"


def test_kb_needs_a_session() -> None:
    settings = Settings(env="test")
    http = TestClient(create_app(settings, create_engine(settings), FakeAsyncRedis()))

    assert http.get("/kb/documents").status_code == 401
    assert http.post("/kb/reindex").status_code == 401
