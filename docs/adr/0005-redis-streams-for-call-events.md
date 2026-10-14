# ADR 0005: Redis Streams for call events, pub/sub for signals

- Status: Accepted
- Date: 2026-10-14

## Context

The gateway produces call events (turns, tool calls, timings, handoffs) that must reach
the `call_events` table for the call log (FR-13) even if the admin API restarts. Other
messages are mere signals: "a new version was published", live log updates in the UI.

## Decision

Call events go to the Redis Stream `call-events` (`MAXLEN ~ 100k`). The admin API reads
it with consumer group `persist`, writes to `call_events` idempotently (stream entry id
is the unique primary key), acknowledges with `XACK`, and reclaims stuck entries with
`XAUTOCLAIM` on start. Pub/sub is used only where loss is harmless: `bot:published` and
`calls:live`.

## Alternatives considered

- **Pub/sub only.** Messages are lost while no subscriber is connected.
- **Kafka (or another broker).** Durable and scalable, but a heavy extra service for the
  volume of a demo; Redis is already required.

## Consequences

- At-least-once delivery; consumers must be idempotent.
- Trace context travels in the stream entry so persistence joins the call's trace.
- Stream length is capped; events older than the cap exist only in PostgreSQL.
