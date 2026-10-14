# ADR 0001: Separate gateway and admin API services in one repository

- Status: Accepted
- Date: 2026-10-14

## Context

The platform has two very different workloads. The dialogue gateway holds long-lived
WebSocket sessions, streams LLM output and scales with the number of concurrent calls.
The admin API is ordinary request/response CRUD (bots, drafts, versions, call logs, auth)
plus background eval jobs. They also change at different rates: gateway releases must
drain live calls, admin releases need not.

## Decision

Run two FastAPI services, `apps/gateway` and `apps/admin-api`, from one monorepo (uv
workspace). They share code only through `packages/*` and share state only through
PostgreSQL and Redis. Each has its own Docker image and Railway service.

## Alternatives considered

- **One monolith serving both WebSocket and admin endpoints.** Simpler to deploy, but a
  deploy of an admin fix would drop or drain live calls, and scaling for calls would
  scale the CRUD API with it.
- **Separate repositories.** Contracts would need publishing and versioning; too much
  overhead for one developer.

## Consequences

- Two images, two health checks, two sets of env vars to manage.
- Cross-service communication is asynchronous through Redis (ADR 0005), which forces
  explicit contracts.
- Graceful shutdown and drain settings matter only for the gateway.
