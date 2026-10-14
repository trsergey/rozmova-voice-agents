"""Export JSON Schema for the contracts to ``packages/contracts/schema/``.

Run with ``make schemas``. The files are committed; a contract test fails when they
are out of date, so the admin web types cannot silently drift from the models.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from rozmova_contracts.protocol import client_message_adapter, server_message_adapter

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

SCHEMAS: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "protocol.client.json": ("ClientMessage", client_message_adapter),
    "protocol.server.json": ("ServerMessage", server_message_adapter),
}


def render(title: str, adapter: TypeAdapter[Any]) -> str:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        **adapter.json_schema(mode="serialization"),
    }
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for filename, (title, adapter) in SCHEMAS.items():
        path = SCHEMA_DIR / filename
        path.write_text(render(title, adapter), encoding="utf-8")
        print(f"wrote {path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
