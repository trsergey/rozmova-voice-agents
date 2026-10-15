# WebSocket protocol

Reference for clients of the dialogue gateway: the demo widget, the admin test console and
any other text client. It describes the wire format; the design behind it lives in the SDD
sections "Протокол WebSocket" and "Діалоговий гейтвей", and in
[ADR 0010](adr/0010-demo-access-codes-and-signed-tickets.md) for tickets.

Source of truth:

- Pydantic models: [`packages/contracts/src/rozmova_contracts/protocol.py`](../packages/contracts/src/rozmova_contracts/protocol.py)
- JSON Schema (generated): [`protocol.client.json`](../packages/contracts/schema/protocol.client.json),
  [`protocol.server.json`](../packages/contracts/schema/protocol.server.json)
- TypeScript types (generated): [`protocol.client.d.ts`](../packages/contracts/ts/protocol.client.d.ts),
  [`protocol.server.d.ts`](../packages/contracts/ts/protocol.server.d.ts)

Regenerate the schemas and TS types with `make schemas`; never edit them by hand. Every
`json` example below is validated against the models by
`packages/contracts/tests/test_protocol_doc.py`, so this page cannot drift from the code.

## Connecting

```
WS /ws/calls/{bot_id}?call_id={uuid}&ticket={ticket}
```

1. The client asks the admin API for a ticket: `POST /calls/ticket`. The ticket is
   HMAC-signed and carries `user_id`, `bot_id`, `call_id`, a `debug` flag and a 60 s expiry.
   It is single-use.
2. The client opens the WebSocket with the same `call_id` and the ticket. The admin test
   console also adds `?draft=true` to talk to the bot's draft instead of the published
   version.
3. Before any session work the gateway checks the `Origin` header against the allowed
   domains (a client without `Origin` is not a browser and is held by the ticket alone),
   the signature, the expiry, that the ticket matches `bot_id` and `call_id`, and single
   use. On failure it accepts the handshake and closes at once with code `4401` (see
   [Close codes](#close-codes)); no message is sent first.
4. On success the server sends `session_started`, then the greeting of the flow's `start`
   node as `assistant_delta` events.

To **resume** a dropped call, request a new ticket for the same `call_id` and connect again
while the session state is alive (30 minutes after the last turn). The server answers with
`session_started` where `resumed` is `true` and `history` holds the conversation so far; the
client re-renders it. A turn that was running when the connection dropped is cancelled and
its partial reply appears in `history` with `interrupted: true`.

## Framing rules

- Every frame is a single JSON text message with a `type` field. Client and server messages
  are two separate discriminated unions.
- Unknown fields are rejected. An unknown `type`, a missing field or invalid JSON from the
  client produces an `error` event with code `invalid_message`; the connection stays open.
- `turn_id` is a 1-based turn number within the call. All `assistant_delta`, `tool_call`,
  `tool_result`, `turn_end` and `turn_cancelled` events of a turn carry the same `turn_id`.
- The server sends WebSocket ping frames every 15 s; they are transport-level, not JSON
  messages.

## Client → server

| `type` | Fields | When |
| --- | --- | --- |
| `user_text` | `text` (1–2000 chars) | The subscriber says something. Starts a turn, or cancels the running turn and starts a new one. |
| `audio_chunk` | `pcm16` (base64), `seq` (≥ 0) | Reserved for the voice channel after the MVP. |
| `hangup` | — | The subscriber ends the call. |

### `user_text`

```json
{"type": "user_text", "text": "скільки в мене грошей?"}
```

### `audio_chunk`

Not used by the MVP text channel. `pcm16` is base64-encoded 16-bit PCM; `seq` orders the
chunks.

```json
{"type": "audio_chunk", "pcm16": "AAEAAgADAAQ=", "seq": 0}
```

### `hangup`

```json
{"type": "hangup"}
```

The server answers with `session_end` (reason `hangup`) and closes the connection.

## Server → client

| `type` | Fields | When | Debug only |
| --- | --- | --- | --- |
| `session_started` | `call_id`, `bot_version`, `resumed`, `history` | Right after connect: session created or resumed | no |
| `assistant_delta` | `turn_id`, `text` | A streamed fragment of the bot reply | no |
| `tool_call` | `turn_id`, `name`, `args` | The engine invoked a tool | **yes** |
| `tool_result` | `turn_id`, `name`, `result` | The tool returned | **yes** |
| `node_enter` | `node_id` | The flow executor entered a node | **yes** |
| `turn_end` | `turn_id`, `timings`, `sources` | The turn finished | no |
| `turn_cancelled` | `turn_id` | The turn was interrupted by a new `user_text` | no |
| `handoff` | `reason` | The call is transferred to a human operator | no |
| `error` | `code`, `message`, `retryable` | A typed error; see [Error codes](#error-codes) | no |
| `session_end` | `reason` | The session is closed; see [Session end reasons](#session-end-reasons) | no |

### `session_started`

`bot_version` is the published version number, or `null` when the client talks to the draft
(test console). `history` is present only when `resumed` is `true`.

New session:

```json
{
  "type": "session_started",
  "call_id": "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c",
  "bot_version": 3,
  "resumed": false,
  "history": null
}
```

Resumed session:

```json
{
  "type": "session_started",
  "call_id": "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c",
  "bot_version": 3,
  "resumed": true,
  "history": [
    {"role": "assistant", "text": "Вітаю! Я асистент Hvylia Mobile. Чим можу допомогти?", "interrupted": false},
    {"role": "user", "text": "що входить у тариф Безлім?", "interrupted": false},
    {"role": "assistant", "text": "Тариф Безлім включає безлімітний інтернет", "interrupted": true}
  ]
}
```

### `assistant_delta`

Text fragments arrive as soon as the LLM produces them; the client appends them to the reply
of `turn_id`. Under backpressure the server may merge several small deltas into one.

```json
{"type": "assistant_delta", "turn_id": 2, "text": "На вашому рахунку "}
```

### `tool_call` (debug only)

```json
{"type": "tool_call", "turn_id": 2, "name": "get_balance", "args": {}}
```

```json
{"type": "tool_call", "turn_id": 4, "name": "activate_service", "args": {"service_id": "roaming_eu"}}
```

### `tool_result` (debug only)

`result` is any JSON value returned by the tool.

```json
{"type": "tool_result", "turn_id": 2, "name": "get_balance", "result": {"balance_uah": 120.5}}
```

```json
{
  "type": "tool_result",
  "turn_id": 4,
  "name": "activate_service",
  "result": {"status": "confirmation_required", "service": "roaming_eu", "price": 99}
}
```

### `node_enter` (debug only)

Sent on every entry into a flow node; the test console highlights the node.

```json
{"type": "node_enter", "node_id": "tariff_kb"}
```

### `turn_end`

Closes a turn. `timings` shows where the latency went (milliseconds):

- `total_ms`: the whole turn.
- `ttft_ms`: from accepting `user_text` to the first `assistant_delta`; `null` if the turn
  produced no text.
- `stages`: per-stage durations, e.g. `state_load`, `embedding`, `search`, `llm_ttft`,
  `tools`.

`sources` lists the knowledge-base chunks cited in the reply as `[1]`, `[2]`, … (`marker`
is the number in brackets); it is empty when the reply cites nothing.

```json
{
  "type": "turn_end",
  "turn_id": 3,
  "timings": {
    "total_ms": 1480.5,
    "ttft_ms": 910.2,
    "stages": {"state_load": 12.0, "embedding": 140.3, "search": 21.7, "llm_ttft": 640.0}
  },
  "sources": [
    {
      "marker": 1,
      "chunk_id": "tariffs-bezlim-1",
      "document_id": "tariffs",
      "document_title": "Тарифи Hvylia Mobile",
      "section": "Безлім"
    }
  ]
}
```

### `turn_cancelled`

A new `user_text` arrived while the turn was still running (FR-4). The server stops the
turn, sends `turn_cancelled` for it and starts a new turn for the new utterance. The client
should mark the partial reply as interrupted; the server keeps it in history with
`interrupted: true` so the LLM knows it was cut off. A side-effect tool that already started
(for example `activate_service`) is finished first and only then is the turn cancelled.

```json
{"type": "turn_cancelled", "turn_id": 2}
```

### `handoff`

The flow reached a `handoff` node (or the executor gave up, e.g. too many transitions in one
turn); `reason` comes from the node. The call ends after it.

```json
{"type": "handoff", "reason": "requested_or_unresolved"}
```

### `error`

A typed error. The connection stays open unless a `session_end` follows. `message` is for
logs and developers, not for the subscriber. `retryable: true` means the same request may
succeed if the client sends it again.

```json
{"type": "error", "code": "turn_timeout", "message": "Turn exceeded 30 s", "retryable": true}
```

```json
{"type": "error", "code": "invalid_message", "message": "Unknown message type 'user_speech'", "retryable": false}
```

### `session_end`

The last message of a session; the server closes the connection after it.

```json
{"type": "session_end", "reason": "completed"}
```

```json
{"type": "session_end", "reason": "shutdown"}
```

## Error codes

`error.code` is one of:

| `code` | Meaning |
| --- | --- |
| `invalid_message` | The client frame is not valid JSON or does not match the client message schema. |
| `turn_timeout` | A bounded wait expired: the whole turn (30 s), the first LLM token (10 s) or a tool call (5 s). |
| `llm_unavailable` | The LLM provider failed or is unreachable after retries. |
| `tool_failed` | A tool raised an error. |
| `rate_limited` | The per-user message limit of the demo was exceeded (20 messages per minute). |
| `internal` | Any other server error; details are in the server logs under the `call_id`. |

## Session end reasons

`session_end.reason` is one of:

| `reason` | Meaning |
| --- | --- |
| `hangup` | The client sent `hangup`. |
| `timeout` | The session idled out. |
| `shutdown` | The gateway is restarting (new deploy). The state is saved; reconnect with the same `call_id` and a new ticket. |
| `limit` | A demo session limit was hit: 10 minutes or 30 turns per session. |
| `handoff` | The call was transferred to an operator (follows a `handoff` event). |
| `completed` | The flow reached its `end` node. |

## Close codes

The protocol models do not define close codes; they are set by the gateway, per the SDD.
Refusals are sent after `accept()`: closing before it makes the server answer the
handshake with HTTP 403, and a browser would see only code `1006`.

| Code | When |
| --- | --- |
| `4401` | The ticket is missing, invalid, expired, already used, does not match `bot_id`/`call_id`, or the `Origin` is not allowed. Get a new ticket. Sent before any message. |
| `4404` | The ticket is valid but there is no such bot. Sent before any message. |
| `1013` | Backpressure: the client did not read and the server's outbound queue stayed full for more than 10 s. |

During graceful shutdown the gateway refuses new connections on `/ws` with HTTP `503`
(before the WebSocket upgrade), lets running turns finish for up to 60 s, then sends
`session_end` with reason `shutdown` to every open session.

## Debug mode

`tool_call`, `tool_result` and `node_enter` reveal the bot's internals, so the gateway sends
them only when the ticket has `debug: true`. The admin API sets that flag for the test
console (`?draft=true`) and for admins. The public demo widget receives only text,
`turn_end`, `turn_cancelled`, `handoff`, `error` and session events; a client must ignore
debug events it does not expect. In the models these events have `debug_only = True`.

## A typical session

`C:` client to server, `S:` server to client, `(debug)` sent only in debug mode. The greeting
is shown as turn 1 here; the first `user_text` starts turn 2.

```text
C: connect /ws/calls/{bot_id}?call_id=3f2c…&ticket=…
S: {"type": "session_started", "call_id": "3f2c…", "bot_version": 3, "resumed": false, "history": null}
S: {"type": "node_enter", "node_id": "start"}                                         (debug)
S: {"type": "assistant_delta", "turn_id": 1, "text": "Вітаю! Я асистент Hvylia Mobile. "}
S: {"type": "assistant_delta", "turn_id": 1, "text": "Чим можу допомогти?"}
S: {"type": "node_enter", "node_id": "assist"}                                        (debug)
S: {"type": "turn_end", "turn_id": 1, "timings": {...}, "sources": []}

# S1: balance, a turn with a tool call
C: {"type": "user_text", "text": "скільки в мене грошей?"}
S: {"type": "tool_call", "turn_id": 2, "name": "get_balance", "args": {}}             (debug)
S: {"type": "tool_result", "turn_id": 2, "name": "get_balance", "result": {...}}      (debug)
S: {"type": "assistant_delta", "turn_id": 2, "text": "На вашому рахунку "}
S: {"type": "assistant_delta", "turn_id": 2, "text": "120,50 грн."}
S: {"type": "turn_end", "turn_id": 2, "timings": {...}, "sources": []}

# S2 interrupted: a new user_text cancels the running turn
C: {"type": "user_text", "text": "що входить у тариф Безлім?"}
S: {"type": "node_enter", "node_id": "tariff_kb"}                                     (debug)
S: {"type": "assistant_delta", "turn_id": 3, "text": "Тариф Безлім включає "}
C: {"type": "user_text", "text": "а скільки він коштує?"}
S: {"type": "turn_cancelled", "turn_id": 3}
S: {"type": "assistant_delta", "turn_id": 4, "text": "Тариф Безлім коштує … [1]"}
S: {"type": "turn_end", "turn_id": 4, "timings": {...}, "sources": [{"marker": 1, ...}]}

# a failed turn: typed error, the connection stays open
C: {"type": "user_text", "text": "підключіть роумінг Європа"}
S: {"type": "error", "code": "llm_unavailable", "message": "...", "retryable": true}

# goodbye
C: {"type": "user_text", "text": "дякую, до побачення"}
S: {"type": "node_enter", "node_id": "bye"}                                           (debug)
S: {"type": "assistant_delta", "turn_id": 6, "text": "Дякую за дзвінок. Гарного дня!"}
S: {"type": "session_end", "reason": "completed"}
S: close
```

A handoff ends the same way: `handoff` followed by `session_end` with reason `handoff`.

## Not implemented yet

The contracts above are final for the MVP; the gateway that speaks them is not built yet.
Points the gateway ticket must settle and then reflect here:

- Close codes (`4401`, `1013`) and the HTTP `503` during shutdown come from the SDD, not from
  `packages/contracts`.
- Whether the greeting is its own turn (`turn_id` 1, closed by `turn_end`) as shown above;
  the SDD walkthrough lists only `session_started` and `assistant_delta` for the call start.
- Whether a failed turn is also closed by `turn_end`; the SDD only requires the `error`
  event.
