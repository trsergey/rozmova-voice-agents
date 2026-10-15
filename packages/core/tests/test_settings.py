from pathlib import Path

import pytest
from pydantic import SecretStr

from rozmova_core.settings import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_reads_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROZMOVA_SERVICE_NAME", "gateway")
    monkeypatch.setenv("ROZMOVA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ROZMOVA_REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("SERVICE_NAME", "ignored-without-prefix")

    settings = Settings()

    assert settings.service_name == "gateway"
    assert settings.log_level == "DEBUG"
    assert settings.redis_url.get_secret_value() == "redis://cache:6379/1"


def test_secrets_are_masked_in_repr_and_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROZMOVA_TICKET_SECRET", "s3cr3t-ticket")
    monkeypatch.setenv("ROZMOVA_DATABASE_URL", "postgresql+asyncpg://u:dbpass@db/rozmova")

    settings = Settings()

    assert isinstance(settings.ticket_secret, SecretStr)
    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert "s3cr3t-ticket" not in text
        assert "dbpass" not in text


def test_otel_endpoint_uses_standard_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().otel_exporter_otlp_endpoint is None

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")

    assert Settings().otel_exporter_otlp_endpoint == "http://jaeger:4318"


def test_openai_key_uses_standard_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().openai_api_key is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    key = Settings().openai_api_key
    assert key is not None
    assert key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(Settings())
