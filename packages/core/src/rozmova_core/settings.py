"""Process settings read from the environment (prefix ``ROZMOVA_``)."""

from functools import lru_cache
from typing import Literal

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
