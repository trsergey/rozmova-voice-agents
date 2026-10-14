# ADR 0002: Session as an asyncio TaskGroup, state in Redis

- Status: Accepted
- Date: 2026-10-14

## Context

A call is one WebSocket connection that must read user input, run turns that stream LLM
output, write events with backpressure and ping, and cancel a running turn when the
subscriber interrupts (FR-4). Session state must survive a reconnect or a gateway restart
(FR-21).

## Decision

Each connection is a `Session` running three tasks under one `asyncio.TaskGroup`:
`reader` (validate and enqueue into a bounded `inbox`), `turn_loop` (run each turn as a
separate `current_turn` task and cancel it on new input) and `writer` (drain a bounded
`outbox`, ping every 15 s). Failure of any task closes the session. History, current
node, slots and `pending_action` are written to Redis (`session:{call_id}`, TTL 30 min)
after every turn, so a client can resume with the same `call_id`.

## Alternatives considered

- **A worker process per session.** Strong isolation, but heavy per call and needs
  inter-process plumbing for streaming and cancellation.
- **A task queue (Celery/RQ/arq) per turn.** Adds latency and makes token streaming and
  mid-turn cancellation awkward.

## Consequences

- Cancellation, timeouts and shutdown use standard asyncio primitives; code must follow
  the rules in `CLAUDE.md` (never swallow `CancelledError`, `shield()` for side effects).
- One gateway process handles many sessions; the target is 50 concurrent sessions per
  instance (load test, ROZ-77).
- Session state is only as durable as Redis; acceptable for a demo.
