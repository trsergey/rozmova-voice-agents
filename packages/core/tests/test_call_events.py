from datetime import UTC, datetime
from uuid import uuid4

import pytest

from rozmova_core.call_events import CallEvent


def test_an_entry_round_trips() -> None:
    event = CallEvent(
        call_id=uuid4(),
        bot_id=uuid4(),
        type="user_message",
        payload={"text": "скільки в мене грошей"},
        ts=datetime(2026, 10, 16, 19, 0, tzinfo=UTC),
        trace_id="a" * 32,
        span_id="b" * 16,
    )

    fields = event.fields()
    assert fields["payload"] == '{"text":"скільки в мене грошей"}'
    assert fields["ts"] == "2026-10-16T19:00:00+00:00"
    assert CallEvent.from_fields(fields) == event


def test_trace_fields_are_optional() -> None:
    event = CallEvent(call_id=uuid4(), bot_id=uuid4(), type="node_enter", payload={})
    fields = event.fields()
    assert "trace_id" not in fields
    assert CallEvent.from_fields(fields).trace_id is None


@pytest.mark.parametrize(
    ("broken", "match"),
    [
        ({"payload": "[1]"}, "not a JSON object"),
        ({"payload": "not json"}, "Expecting value"),
        ({"call_id": "nope"}, "badly formed"),
        ({"ts": "yesterday"}, "isoformat"),
    ],
)
def test_a_malformed_entry_raises(broken: dict[str, str], match: str) -> None:
    fields = CallEvent(call_id=uuid4(), bot_id=uuid4(), type="x", payload={}).fields() | broken
    with pytest.raises(ValueError, match=match):
        CallEvent.from_fields(fields)


def test_a_missing_field_raises() -> None:
    fields = CallEvent(call_id=uuid4(), bot_id=uuid4(), type="x", payload={}).fields()
    del fields["type"]
    with pytest.raises(KeyError):
        CallEvent.from_fields(fields)
