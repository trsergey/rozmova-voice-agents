"""Export the admin API's OpenAPI schema to ``apps/admin-api/openapi.json``.

``make client`` runs it and then generates the admin-web's TS client types from the file
with ``openapi-typescript`` (SDD "Admin API і адмінка"). Both are committed; a test and the
CI ``contracts`` job fail when they are stale, so an API change breaks the frontend build
instead of production.
"""

import json
from pathlib import Path

from redis.asyncio import Redis

from admin_api.app import create_app
from rozmova_core.db import create_engine
from rozmova_core.settings import Settings

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def render() -> str:
    # Neither the engine nor the Redis client connects until used; no lifespan runs here.
    settings = Settings(env="test")
    app = create_app(settings, create_engine(settings), Redis())
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SCHEMA_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
