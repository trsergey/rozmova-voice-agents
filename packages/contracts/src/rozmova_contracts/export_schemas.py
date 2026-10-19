"""Export JSON Schema for the contracts to ``packages/contracts/schema/``.

Run with ``make schemas``, which then generates TS types from these files into
``packages/contracts/ts/`` for the admin web, and zod schemas of the flow node settings
into ``apps/admin-web/src/api/flow.zod.ts`` for the editor's forms (``--deref`` prints one
definition with its references inlined, which the zod generator needs). All are committed;
a contract test fails when the JSON Schema is out of date, so the models and the schema
cannot silently drift.
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

from rozmova_contracts.flow import flow_graph_adapter
from rozmova_contracts.protocol import client_message_adapter, server_message_adapter

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

SCHEMAS: dict[str, tuple[str, TypeAdapter[Any]]] = {
    "protocol.client.json": ("ClientMessage", client_message_adapter),
    "protocol.server.json": ("ServerMessage", server_message_adapter),
    "flow.json": ("FlowGraph", flow_graph_adapter),
}


class _NoFieldTitles(GenerateJsonSchema):
    """Skip per-field titles; otherwise each field becomes a named alias in the TS output."""

    def field_title_should_be_set(self, schema: object) -> bool:
        return False


def render(title: str, adapter: TypeAdapter[Any]) -> str:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        **adapter.json_schema(mode="serialization", schema_generator=_NoFieldTitles),
    }
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


# The flow definitions the editor's forms validate: the settings of each node type.
ZOD_DEFS = (
    "StartData",
    "LLMPromptData",
    "ToolData",
    "KBAnswerData",
    "ConditionData",
    "HandoffData",
    "EndData",
)


def deref(name: str) -> dict[str, Any]:
    """A ``$defs`` entry of the flow schema with every ``$ref`` replaced by its target."""
    schema = flow_graph_adapter.json_schema(mode="serialization", schema_generator=_NoFieldTitles)
    defs: dict[str, Any] = schema["$defs"]

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                return inline(defs[ref.removeprefix("#/$defs/")])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    resolved: dict[str, Any] = inline(defs[name])
    return resolved


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--deref":
        print(json.dumps(deref(sys.argv[2]), ensure_ascii=False))
        return
    SCHEMA_DIR.mkdir(exist_ok=True)
    for filename, (title, adapter) in SCHEMAS.items():
        path = SCHEMA_DIR / filename
        path.write_text(render(title, adapter), encoding="utf-8")
        print(f"wrote {path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
