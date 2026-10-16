"""admin-api serves the built admin-web under /demo/ when ``admin_web_dir`` is set (ROZ-68).
No lifespan runs here (``TestClient`` without ``with``), so no database is needed."""

from pathlib import Path

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from admin_api.app import create_app
from rozmova_core.db import create_engine
from rozmova_core.settings import Settings


def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, create_engine(settings), FakeAsyncRedis()))


def built_page(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    return tmp_path


def test_demo_page_and_assets_are_served(tmp_path: Path) -> None:
    http = client(Settings(env="test", admin_web_dir=built_page(tmp_path)))

    page = http.get("/demo/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'id="root"' in page.text

    asset = http.get("/demo/assets/index-abc.js")
    assert asset.status_code == 200
    assert http.get("/demo/assets/missing.js").status_code == 404


def test_root_redirects_to_the_demo_page(tmp_path: Path) -> None:
    http = client(Settings(env="test", admin_web_dir=built_page(tmp_path)))

    response = http.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/demo/"


def test_the_api_still_answers_next_to_the_page(tmp_path: Path) -> None:
    http = client(Settings(env="test", admin_web_dir=built_page(tmp_path)))

    assert http.get("/healthz").json() == {"status": "ok"}


def test_no_page_without_admin_web_dir() -> None:
    http = client(Settings(env="test"))

    assert http.get("/demo/").status_code == 404
    assert http.get("/").status_code == 404
