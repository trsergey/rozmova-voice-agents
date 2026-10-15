"""Per-call dialogue state and its Redis hash form.

The gateway saves ``SessionState`` to ``session:{call_id}`` after every turn (SDD "Стан
сесії") so a client can reconnect with the same call_id (FR-21); the flow executor reads and
updates it; evals run on the same object. TTL and the Redis calls themselves belong to the
caller: this module only converts to and from hash fields.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from rozmova_contracts.protocol import HistoryMessage
from rozmova_engine.providers import Message

# Bump when the stored shape changes; sessions saved by another version are not resumed.
FORMAT_VERSION = "1"

# Appended to a cut-off reply in the LLM history, so the model knows it was interrupted.
INTERRUPTED_MARK = "[абонент перебив цю відповідь]"

# What HGETALL returns: str with decode_responses=True, bytes without; redis-py types it as both.
RedisHash = Mapping[str, str] | Mapping[bytes, bytes] | Mapping[str | bytes, str | bytes]


class PendingAction(BaseModel):
    """A side-effect tool call waiting for the subscriber's confirmation (SDD, S3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    args: dict[str, JsonValue]
    turn: int = Field(ge=1, description="Turn in which the price was announced.")


class HistoryEntry(BaseModel):
    """One message of the call with the turn it belongs to; the greeting is turn 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int = Field(ge=0)
    message: Message
    interrupted: bool = False

    @model_validator(mode="after")
    def _only_assistant_is_interrupted(self) -> Self:
        if self.interrupted and self.message.role != "assistant":
            raise ValueError("only assistant replies can be interrupted")
        return self


class SessionStateError(ValueError):
    """Stored state cannot be read: another format version, missing or corrupt fields."""


class SessionState(BaseModel):
    """Everything needed to continue a call. Mutable: the executor updates it in place."""

    model_config = ConfigDict(extra="forbid")

    call_id: UUID
    bot_id: UUID
    # The call keeps the version it started on, even if a newer one is published meanwhile.
    bot_version: int | None = Field(description="Published version number; null for a draft.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the call started; a resumed call keeps it (call duration limit).",
    )
    turn: int = Field(default=0, ge=0, description="Number of the last started turn.")
    current_node: str | None = Field(default=None, description="Null until `start` has run.")
    slots: dict[str, JsonValue] = Field(default_factory=dict)
    pending_action: PendingAction | None = None
    history: list[HistoryEntry] = Field(default_factory=list)

    def llm_messages(self) -> list[Message]:
        """History as the LLM sees it, with interrupted replies marked."""
        messages = []
        for entry in self.history:
            message = entry.message
            if entry.interrupted:
                content = f"{message.content} {INTERRUPTED_MARK}".lstrip()
                message = message.model_copy(update={"content": content})
            messages.append(message)
        return messages

    def transcript(self) -> list[HistoryMessage]:
        """What the subscriber saw, for ``session_started{resumed: true}``: no tool traffic."""
        return [
            HistoryMessage(
                role=entry.message.role,
                text=entry.message.content,
                interrupted=entry.interrupted,
            )
            for entry in self.history
            if entry.message.role in ("user", "assistant") and entry.message.content
        ]

    def to_redis(self) -> dict[str, str]:
        """Hash fields for ``HSET session:{call_id}``: one JSON value per model field."""
        data = self.model_dump(mode="json")
        return {"v": FORMAT_VERSION} | {name: json.dumps(value) for name, value in data.items()}

    @classmethod
    def from_redis(cls, fields: RedisHash) -> Self:
        """Rebuild the state from ``HGETALL``; accepts decoded and raw responses."""
        decoded = {_text(key): _text(value) for key, value in fields.items()}
        version = decoded.pop("v", None)
        if version != FORMAT_VERSION:
            raise SessionStateError(f"unsupported session format {version!r}")
        try:
            return cls.model_validate({name: json.loads(raw) for name, raw in decoded.items()})
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SessionStateError(f"corrupt session state: {exc}") from exc


def _text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value
