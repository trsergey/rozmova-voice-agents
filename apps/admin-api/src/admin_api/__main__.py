"""``python -m admin_api``: the admin API on ``$PORT`` (8000 by default)."""

import os

import uvicorn
from fastapi import FastAPI

from admin_api.app import create_app
from rozmova_core.log import configure_logging
from rozmova_core.settings import Settings


def build() -> FastAPI:
    configure_logging(Settings())
    return create_app()


def main() -> None:
    uvicorn.run(
        "admin_api.__main__:build",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - a container service listens on all interfaces
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
