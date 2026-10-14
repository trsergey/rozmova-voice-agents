# ADR 0011: Dialogue engine as the shared package packages/engine

- Status: Accepted
- Date: 2026-10-14

## Context

The same dialogue logic runs in three places: live calls in the gateway, eval runs in the
admin API before publishing (FR-19), and the eval suite in CI. If evals drove the bot
through a different path than production, a green eval would prove little.

## Decision

The flow executor, LLM providers, tools, RAG and the eval runner live in
`packages/engine` with no WebSocket or HTTP code. The gateway is a thin WebSocket layer
around it; the admin API and CI call the same engine directly.

## Alternatives considered

- **Engine inside the gateway, evals over WebSocket.** Tests the full path, but evals then
  need a running gateway, tickets and network in CI, and are slower and flakier.

## Consequences

- Evals exercise exactly the production executor.
- The engine API (turn in, events out via `emit`) is a contract that both apps depend on.
- WebSocket-specific behaviour (backpressure, reconnect) is covered by gateway integration
  tests instead.
