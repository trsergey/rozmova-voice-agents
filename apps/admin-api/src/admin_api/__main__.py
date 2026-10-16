"""``python -m admin_api``: the admin API on ``$PORT`` (8000 by default)."""

import os

import uvicorn
from fastapi import FastAPI

from admin_api.app import create_app
from rozmova_core.db import create_engine
from rozmova_core.log import configure_logging
from rozmova_core.redis_client import create_redis
from rozmova_core.settings import Settings
from rozmova_core.telemetry import init_telemetry, shutdown_telemetry


def build() -> FastAPI:
    settings = Settings()
    configure_logging(settings)
    # Traces only with OTEL_EXPORTER_OTLP_ENDPOINT; without it the tracer stays a no-op.
    init_telemetry(settings)
    return create_app(settings, create_engine(settings), create_redis(settings))


def main() -> None:
    try:
        uvicorn.run(
            "admin_api.__main__:build",
            factory=True,
            host="0.0.0.0",  # noqa: S104 - a container service listens on all interfaces
            port=int(os.environ.get("PORT", "8000")),
            log_config=None,
        )
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
