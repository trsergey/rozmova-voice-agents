"""WebSocket protocol between the dialogue gateway and its clients.

Every frame is a JSON object with a ``type`` field. Client and server messages are
separate discriminated unions, so an unknown or malformed ``type`` fails validation
and the gateway answers with an ``error`` event instead of dropping the connection.
See the SDD section "Протокол WebSocket" for the message table.
"""

from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

# Frames are serialized with every field, so the exported schema marks defaulted
# fields (including the ``type`` discriminator) as required.
_CONFIG = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)

TurnId = Annotated[int, Field(ge=1, description="1-based turn number within the call.")]


class ProtocolMessage(BaseModel):
    """Base for every frame; ``debug_only`` marks events hidden from public clients."""

    model_config = _CONFIG

    # Events that reveal bot internals; the gateway sends them only when the ticket
    # has the debug flag (test console, admins).
    debug_only: ClassVar[bool] = False


# --- client → server -------------------------------------------------------------


class UserText(ProtocolMessage):
    """A subscriber utterance. Starts a turn, or cancels the running one and starts anew."""

    type: Literal["user_text"] = "user_text"
    text: str = Field(min_length=1, max_length=2000)


class AudioChunk(ProtocolMessage):
    """A chunk of 16-bit PCM audio. Reserved for the voice channel after the MVP."""

    type: Literal["audio_chunk"] = "audio_chunk"
    pcm16: str = Field(min_length=1, description="Base64-encoded 16-bit PCM.")
    seq: int = Field(ge=0)


class Hangup(ProtocolMessage):
    """The subscriber ends the call."""

    type: Literal["hangup"] = "hangup"


ClientMessage = Annotated[UserText | AudioChunk | Hangup, Field(discriminator="type")]


# --- server → client -------------------------------------------------------------


class HistoryMessage(BaseModel):
    """One rendered line of the conversation, sent when a session is resumed."""

    model_config = _CONFIG

    role: Literal["user", "assistant"]
    text: str
    interrupted: bool = Field(
        default=False, description="The assistant reply was cut off by a new utterance."
    )


class SessionStarted(ProtocolMessage):
    """The session was created, or resumed with the same call_id."""

    type: Literal["session_started"] = "session_started"
    call_id: UUID
    bot_version: int | None = Field(description="Published version number; null for a draft.")
    resumed: bool = False
    history: list[HistoryMessage] | None = Field(
        default=None, description="Present only when resumed."
    )


class AssistantDelta(ProtocolMessage):
    """A streamed fragment of the assistant reply."""

    type: Literal["assistant_delta"] = "assistant_delta"
    turn_id: TurnId
    text: str


class ToolCall(ProtocolMessage):
    """The engine invoked a tool."""

    debug_only: ClassVar[bool] = True

    type: Literal["tool_call"] = "tool_call"
    turn_id: TurnId
    name: str
    args: dict[str, JsonValue]


class ToolResult(ProtocolMessage):
    """The result of a tool invocation."""

    debug_only: ClassVar[bool] = True

    type: Literal["tool_result"] = "tool_result"
    turn_id: TurnId
    name: str
    result: JsonValue


class NodeEnter(ProtocolMessage):
    """The flow executor entered a node."""

    debug_only: ClassVar[bool] = True

    type: Literal["node_enter"] = "node_enter"
    node_id: str


class TurnTimings(BaseModel):
    """Where the turn spent its latency budget, in milliseconds."""

    model_config = _CONFIG

    total_ms: float = Field(ge=0)
    ttft_ms: float | None = Field(
        default=None,
        ge=0,
        description="From accepting user_text to the first assistant_delta; null if no text.",
    )
    stages: dict[str, float] = Field(
        default_factory=dict,
        description="Per-stage durations, e.g. state_load, embedding, search, llm_ttft, tools.",
    )


class Source(BaseModel):
    """A knowledge-base chunk cited in the reply as [marker]."""

    model_config = _CONFIG

    marker: int = Field(ge=1)
    chunk_id: str
    document_id: str
    document_title: str
    section: str | None = None


class TurnEnd(ProtocolMessage):
    """The turn finished."""

    type: Literal["turn_end"] = "turn_end"
    turn_id: TurnId
    timings: TurnTimings
    sources: list[Source] = Field(default_factory=list)


class TurnCancelled(ProtocolMessage):
    """The turn was interrupted by a new utterance."""

    type: Literal["turn_cancelled"] = "turn_cancelled"
    turn_id: TurnId


class Handoff(ProtocolMessage):
    """The call is transferred to a human operator."""

    type: Literal["handoff"] = "handoff"
    reason: str


ErrorCode = Literal[
    "invalid_message",
    "turn_timeout",
    "llm_unavailable",
    "tool_failed",
    "rate_limited",
    "internal",
]


class Error(ProtocolMessage):
    """A typed error. The connection stays open unless a session_end follows."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    retryable: bool = False


SessionEndReason = Literal["hangup", "timeout", "shutdown", "limit", "handoff", "completed"]


class SessionEnd(ProtocolMessage):
    """The session is closed.

    ``shutdown`` means the gateway is restarting; the client may reconnect with the same
    call_id and a new ticket.
    """

    type: Literal["session_end"] = "session_end"
    reason: SessionEndReason


ServerMessage = Annotated[
    SessionStarted
    | AssistantDelta
    | ToolCall
    | ToolResult
    | NodeEnter
    | TurnEnd
    | TurnCancelled
    | Handoff
    | Error
    | SessionEnd,
    Field(discriminator="type"),
]

client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
server_message_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(raw: str | bytes) -> ClientMessage:
    """Parse one client frame; raises ``pydantic.ValidationError`` on any mismatch."""
    return client_message_adapter.validate_json(raw)


def parse_server_message(raw: str | bytes) -> ServerMessage:
    """Parse one server frame; used by tests and Python clients."""
    return server_message_adapter.validate_json(raw)
