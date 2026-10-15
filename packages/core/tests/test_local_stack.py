"""Static checks of the local stack config: `.env.example` and `infra/compose.dev.yml`.

They need no Docker; `make up` is the live check.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from rozmova_core.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE_DEV = REPO_ROOT / "infra" / "compose.dev.yml"

# `KEY=value` or a commented-out `# KEY=` placeholder.
_ENV_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if match := _ENV_LINE.match(line):
            keys.add(match.group(1))
    return keys


def _settings_env_names() -> set[str]:
    prefix = Settings.model_config.get("env_prefix", "")
    names = set()
    for name, field in Settings.model_fields.items():
        if field.validation_alias is not None:
            # AliasChoices: the first choice is the canonical (standard) name.
            names.add(str(field.validation_alias.choices[0]))  # type: ignore[union-attr]
        else:
            names.add(f"{prefix}{name}".upper())
    return names


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in _settings_env_names():
        monkeypatch.delenv(name, raising=False)


def test_env_example_documents_every_setting() -> None:
    assert _env_example_keys() == _settings_env_names()


@pytest.mark.usefixtures("_isolated_env")
def test_env_example_loads_and_leaves_secrets_unset() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]

    assert settings.fernet_master_key is None
    assert settings.ticket_secret is None
    assert isinstance(settings.database_url, SecretStr)


@pytest.mark.usefixtures("_isolated_env")
def test_env_example_matches_compose_credentials() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    pg = _compose()["services"]["postgres"]["environment"]

    url = settings.database_url.get_secret_value()
    expected = (
        f"postgresql+asyncpg://{pg['POSTGRES_USER']}:{pg['POSTGRES_PASSWORD']}"
        f"@localhost:5432/{pg['POSTGRES_DB']}"
    )
    assert url == expected


def test_compose_has_required_services_with_healthchecks() -> None:
    services = _compose()["services"]

    assert set(services) >= {"postgres", "redis", "jaeger", "prometheus", "grafana"}
    for name, service in services.items():
        if "build" in service:
            # Our service images carry their HEALTHCHECK in the Dockerfile.
            dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
            assert "\nHEALTHCHECK " in dockerfile, f"{name}: the Dockerfile has no HEALTHCHECK"
        else:
            assert "healthcheck" in service, f"{name} has no healthcheck"


def test_compose_postgres_is_pgvector_pg16_with_data_volume() -> None:
    postgres = _compose()["services"]["postgres"]

    assert postgres["image"].startswith("pgvector/pgvector:")
    assert postgres["image"].endswith("pg16")
    assert any(v.endswith(":/var/lib/postgresql/data") for v in postgres["volumes"])


def test_compose_images_are_pinned_and_ports_local_only() -> None:
    for name, service in _compose()["services"].items():
        assert ":" in service["image"], f"{name} image has no tag"
        assert not service["image"].endswith(":latest"), f"{name} uses :latest"
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), f"{name} exposes {port} beyond localhost"
