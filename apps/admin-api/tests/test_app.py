from fastapi.testclient import TestClient

from admin_api.app import create_app


def test_healthz() -> None:
    with TestClient(create_app()) as http:
        assert http.get("/healthz").json() == {"status": "ok"}
