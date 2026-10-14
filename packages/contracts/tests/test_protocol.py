import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from rozmova_contracts.export_schemas import SCHEMA_DIR, SCHEMAS, render
from rozmova_contracts.protocol import (
    AssistantDelta,
    AudioChunk,
    Error,
    Handoff,
    Hangup,
    NodeEnter,
    ProtocolMessage,
    SessionEnd,
    SessionStarted,
    ToolCall,
    ToolResult,
    TurnCancelled,
    TurnEnd,
    UserText,
    parse_client_message,
    parse_server_message,
    server_message_adapter,
)

CALL_ID = "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c"

Sample = tuple[dict[str, object], type[ProtocolMessage]]

CLIENT_SAMPLES: list[Sample] = [
    ({"type": "user_text", "text": "скільки в мене грошей"}, UserText),
    ({"type": "audio_chunk", "pcm16": "AAEC", "seq": 0}, AudioChunk),
    ({"type": "hangup"}, Hangup),
]

SERVER_SAMPLES: list[Sample] = [
    (
        {"type": "session_started", "call_id": CALL_ID, "bot_version": 3, "resumed": False},
        SessionStarted,
    ),
    (
        {
            "type": "session_started",
            "call_id": CALL_ID,
            "bot_version": None,
            "resumed": True,
            "history": [
                {"role": "assistant", "text": "Вітаю! Чим можу допомогти?"},
                {"role": "user", "text": "що входить у тариф Безлім?"},
                {"role": "assistant", "text": "Тариф Безлім…", "interrupted": True},
            ],
        },
        SessionStarted,
    ),
    ({"type": "assistant_delta", "turn_id": 1, "text": "На рахунку "}, AssistantDelta),
    ({"type": "tool_call", "turn_id": 1, "name": "get_balance", "args": {}}, ToolCall),
    (
        {
            "type": "tool_result",
            "turn_id": 1,
            "name": "activate_service",
            "result": {"status": "confirmation_required", "service": "roaming_eu", "price": 99},
        },
        ToolResult,
    ),
    ({"type": "node_enter", "node_id": "tariff_kb"}, NodeEnter),
    (
        {
            "type": "turn_end",
            "turn_id": 2,
            "timings": {"total_ms": 1480.5, "ttft_ms": 910.2, "stages": {"llm_ttft": 640.0}},
            "sources": [
                {
                    "marker": 1,
                    "chunk_id": "c-17",
                    "document_id": "tariffs",
                    "document_title": "Тарифи",
                    "section": "Безлім",
                }
            ],
        },
        TurnEnd,
    ),
    ({"type": "turn_cancelled", "turn_id": 3}, TurnCancelled),
    ({"type": "handoff", "reason": "requested_or_unresolved"}, Handoff),
    ({"type": "error", "code": "turn_timeout", "message": "Turn timed out"}, Error),
    ({"type": "session_end", "reason": "shutdown"}, SessionEnd),
]


@pytest.mark.parametrize(("payload", "model"), CLIENT_SAMPLES)
def test_client_samples_validate(payload: dict[str, object], model: type[ProtocolMessage]) -> None:
    assert isinstance(parse_client_message(json.dumps(payload)), model)


@pytest.mark.parametrize(("payload", "model"), SERVER_SAMPLES)
def test_server_samples_round_trip(
    payload: dict[str, object], model: type[ProtocolMessage]
) -> None:
    message = parse_server_message(json.dumps(payload))
    assert isinstance(message, model)
    assert parse_server_message(server_message_adapter.dump_json(message)) == message


def test_session_started_parses_call_id_as_uuid() -> None:
    message = parse_server_message(json.dumps(SERVER_SAMPLES[0][0]))
    assert isinstance(message, SessionStarted)
    assert message.call_id == UUID(CALL_ID)


@pytest.mark.parametrize(
    "raw",
    [
        '{"type": "user_speech", "text": "hi"}',  # unknown type
        '{"text": "hi"}',  # missing type
        '{"type": "user_text", "text": ""}',  # empty utterance
        '{"type": "user_text", "text": "hi", "extra": 1}',  # unknown field
        '{"type": "assistant_delta", "turn_id": 1, "text": "hi"}',  # server-only type
        "not json",
    ],
)
def test_invalid_client_frames_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(raw)


def test_unknown_session_end_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_server_message('{"type": "session_end", "reason": "bored"}')


def test_debug_only_events() -> None:
    debug_only = {model.__name__ for _, model in SERVER_SAMPLES if model.debug_only}
    assert debug_only == {"ToolCall", "ToolResult", "NodeEnter"}


@pytest.mark.parametrize("filename", sorted(SCHEMAS))
def test_committed_schema_is_up_to_date(filename: str) -> None:
    title, adapter = SCHEMAS[filename]
    committed = (SCHEMA_DIR / filename).read_text(encoding="utf-8")
    assert committed == render(title, adapter), "run `make schemas` and commit the result"
