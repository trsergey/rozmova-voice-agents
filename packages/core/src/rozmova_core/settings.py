"""Process settings read from the environment (prefix ``ROZMOVA_``)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Settings shared by the gateway and admin-api.

    Secrets are ``SecretStr``: they print as ``**********`` in ``repr``, logs and dumps,
    and the real value is read only through ``get_secret_value()`` where it is used.
    """

    model_config = SettingsConfigDict(
        env_prefix="ROZMOVA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    service_name: str = "rozmova"
    env: Literal["local", "test", "dev", "demo"] = "local"

    log_level: LogLevel = "INFO"
    log_json: bool = True

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://rozmova:rozmova@localhost:5432/rozmova"
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)

    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0)
    redis_max_connections: int = Field(default=50, ge=1)

    fernet_master_key: SecretStr | None = None
    ticket_secret: SecretStr | None = None
    # Browser origins allowed to open the gateway WebSocket: the admin SPA (served by
    # admin-api) and the Vite dev server. A JSON list in the environment.
    allowed_origins: list[str] = ["http://localhost:8000", "http://localhost:5173"]
    # The gateway's public base URL; admin-api builds the clients' WebSocket URLs from it.
    gateway_public_url: str = "http://localhost:8001"

    # The built admin-web (``apps/admin-web/dist``) that admin-api serves under /demo/;
    # unset serves no page (the API alone, as in tests and the Vite dev setup).
    admin_web_dir: Path | None = None

    # The admin user admin-api creates or updates on start; both unset skips the seed.
    admin_email: str | None = None
    admin_password: SecretStr | None = None
    # Admin web sessions expire after this long without a request.
    admin_session_ttl_seconds: int = Field(default=12 * 3600, ge=60)

    # The reference demo bot (ROZ-54): admin-api seeds it with this id and flow on start;
    # the deploy smoke test and the public widget call it.
    demo_seed: bool = True
    demo_bot_id: UUID = UUID("00000000-0000-0000-0000-00000000d3e0")
    demo_flow_path: Path = Path("flows/hvylia-demo.json")

    # Knowledge base (ROZ-27): Markdown under kb_path is ingested into pgvector with this
    # embedding model; admin-api re-ingests on start when kb_ingest_on_start is set.
    kb_path: Path = Path("kb")
    kb_ingest_on_start: bool = False
    embedding_model: str = "text-embedding-3-small"
    # The standard OpenAI variable, read without the prefix like the OTel endpoint below.
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "ROZMOVA_OPENAI_API_KEY"),
    )

    # Evals over a draft in admin-api (FR-19, ROZ-73): cases and thresholds under evals_path.
    # Publish needs a green eval of the current draft unless the gate is bypassed.
    evals_path: Path = Path("evals")
    eval_gate_bypass: bool = False

    # Per-user demo limits (SDD "Безпека та контроль витрат"), keyed by user_id from the ticket.
    limit_messages_per_minute: int = Field(default=20, ge=1)
    limit_sessions_per_user: int = Field(default=3, ge=1)
    limit_session_seconds: int = Field(default=600, ge=1)
    limit_turns_per_session: int = Field(default=30, ge=1)

    # Standard OTel variable, read without the prefix so stock tooling keeps working.
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "ROZMOVA_OTEL_EXPORTER_OTLP_ENDPOINT"
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once."""
    return Settings()
