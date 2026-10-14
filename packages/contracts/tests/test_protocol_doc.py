"""Keep docs/protocol.md honest: its examples must validate against the contracts."""

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from rozmova_contracts.protocol import (
    ClientMessage,
    ErrorCode,
    ProtocolMessage,
    ServerMessage,
    SessionEndReason,
    client_message_adapter,
    server_message_adapter,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "protocol.md"
DOC = DOC_PATH.read_text(encoding="utf-8")

JSON_BLOCKS = re.findall(r"^```json\n(.*?)^```$", DOC, flags=re.MULTILINE | re.DOTALL)


def _models(union: Any) -> list[type[ProtocolMessage]]:
    """Message classes of an ``Annotated[A | B | ..., Field(discriminator=...)]`` union."""
    return list(get_args(get_args(union)[0]))


def _type_name(model: type[ProtocolMessage]) -> str:
    name = model.model_fields["type"].default
    assert isinstance(name, str)
    return name


CLIENT_TYPES = {_type_name(m) for m in _models(ClientMessage)}
SERVER_MODELS = {_type_name(m): m for m in _models(ServerMessage)}


def _example_type(block: str) -> str:
    message_type = json.loads(block)["type"]
    assert isinstance(message_type, str)
    return message_type


def _table_codes(heading: str) -> set[str]:
    """First-column backticked values of the table body under ``## <heading>``."""
    section = DOC.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    body = section.split("| --- |", 1)[1]
    return set(re.findall(r"^\| `([a-z_]+)` \|", body, flags=re.MULTILINE))


def test_doc_has_examples() -> None:
    assert len(JSON_BLOCKS) >= len(CLIENT_TYPES) + len(SERVER_MODELS)


@pytest.mark.parametrize("block", JSON_BLOCKS)
def test_every_json_example_validates(block: str) -> None:
    if _example_type(block) in CLIENT_TYPES:
        client_message_adapter.validate_json(block)
    else:
        server_message_adapter.validate_json(block)


def test_every_message_type_has_an_example() -> None:
    documented = {_example_type(block) for block in JSON_BLOCKS}
    assert CLIENT_TYPES | set(SERVER_MODELS) <= documented


def test_debug_only_events_are_marked() -> None:
    for name, model in SERVER_MODELS.items():
        marked = f"### `{name}` (debug only)" in DOC
        assert marked == model.debug_only, name


def test_error_codes_match_contracts() -> None:
    assert _table_codes("Error codes") == set(get_args(ErrorCode))


def test_session_end_reasons_match_contracts() -> None:
    assert _table_codes("Session end reasons") == set(get_args(SessionEndReason))


def test_relative_links_resolve() -> None:
    links = re.findall(r"\]\((\.\./[^)#]+|adr/[^)#]+)\)", DOC)
    assert any(link.endswith("protocol.server.json") for link in links)
    for link in links:
        assert (DOC_PATH.parent / link).resolve().is_file(), link
