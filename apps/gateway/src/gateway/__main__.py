"""``python -m gateway``: the gateway with the static demo bot, for local runs."""

import os

import uvicorn
from fastapi import FastAPI

from gateway.app import create_app
from gateway.bots import DemoBotSettings, StaticBotLoader
from rozmova_core.log import configure_logging
from rozmova_core.settings import Settings

# SDD: ping every 15 s; a client that misses a pong for as long is disconnected.
WS_PING_INTERVAL = 15.0
WS_PING_TIMEOUT = 15.0


def build() -> FastAPI:
    configure_logging(Settings())
    return create_app(StaticBotLoader.from_settings(DemoBotSettings()))


def main() -> None:
    uvicorn.run(
        "gateway.__main__:build",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - a container service listens on all interfaces
        port=int(os.environ.get("PORT", "8001")),
        ws_ping_interval=WS_PING_INTERVAL,
        ws_ping_timeout=WS_PING_TIMEOUT,
        log_config=None,
    )


if __name__ == "__main__":
    main()
