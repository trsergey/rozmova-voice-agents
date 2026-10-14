# Architecture

> Stub. The full English write-up with diagrams lands in ROZ-55. Until then the design lives
> in the Ukrainian documents below.

- [PRD](Rozmova_PRD.md): product requirements, scenarios S1–S5, FR/NFR.
- [SDD](Rozmova_SDD.md): technical design: gateway, protocol, flow semantics, RAG, data
  model, Redis, admin API, security, observability, testing, deployment.
- [ADRs](adr/README.md): decisions and the alternatives we rejected.

## At a glance

```
browser ──HTTPS──▶ admin-api (FastAPI + admin-web SPA) ──▶ PostgreSQL 16 + pgvector
   │                    │  ▲                                     ▲
   │ signed ticket      │  │ call-events stream (persist)        │
   └──WebSocket──▶ gateway (FastAPI, asyncio sessions) ──────────┤
                        │        uses packages/engine            │
                        └──────────▶ Redis 7 ◀───────────────────┘
                                     cache, pub/sub, streams, session state
```

- `gateway`: one asyncio `TaskGroup` per call (reader, turn loop, writer); runs the engine.
- `admin-api`: bots, draft and versions, publish/rollback, call logs, auth, eval jobs.
- `packages/engine`: flow executor, LLM providers, tools, RAG, eval runner; shared by both
  services and CI.
- `packages/contracts`: Pydantic models for the WebSocket protocol and `FlowGraph`; JSON
  Schema and TS types are generated from them.
