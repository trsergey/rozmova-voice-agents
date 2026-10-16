"""The Redis Stream ``call-events`` (ADR 0005): one entry per call event, written by the
gateway (ROZ-52) and persisted by admin-api into ``call_events`` (ROZ-62).

An entry has the fields ``call_id``, ``bot_id``, ``type``, ``ts`` (ISO 8601, UTC),
``payload`` (JSON) and, when a span was recording, ``trace_id`` and ``span_id``. The
types are the protocol's server events (``session_started``, ``node_enter``,
``tool_call``, ``turn_end``…, without ``assistant_delta``) plus ``user_message``,
``assistant_message`` (the streamed reply of a turn, whole) and ``session_closed``.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

STREAM = "call-events"
MAXLEN = 100_000


@dataclass(frozen=True)
class CallEvent:
    call_id: UUID
    bot_id: UUID
    type: str
    payload: Mapping[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    span_id: str | None = None

    def fields(self) -> dict[str, str]:
        """The stream entry."""
        out = {
            "call_id": str(self.call_id),
            "bot_id": str(self.bot_id),
            "type": self.type,
            "ts": self.ts.isoformat(),
            "payload": json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")),
        }
        if self.trace_id is not None:
            out["trace_id"] = self.trace_id
        if self.span_id is not None:
            out["span_id"] = self.span_id
        return out

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> "CallEvent":
        """Parse a stream entry; raises ``KeyError`` or ``ValueError`` on a malformed one."""
        payload = json.loads(fields["payload"])
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        return cls(
            call_id=UUID(fields["call_id"]),
            bot_id=UUID(fields["bot_id"]),
            type=fields["type"],
            payload=payload,
            ts=datetime.fromisoformat(fields["ts"]),
            trace_id=fields.get("trace_id"),
            span_id=fields.get("span_id"),
        )
