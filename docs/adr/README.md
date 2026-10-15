# Architecture Decision Records

Format: [0000-template.md](0000-template.md). The decision table and its context come from
the SDD ("Рішення для ADR і перевірені факти").

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-separate-gateway-and-admin-api.md) | Separate gateway and admin API services in one repository | Accepted |
| [0002](0002-session-on-asyncio-taskgroup.md) | Session as an asyncio TaskGroup, state in Redis | Accepted |
| [0003](0003-own-llm-provider-abstraction.md) | Own LLM provider abstraction, no framework | Accepted |
| [0004](0004-pgvector-hybrid-search.md) | pgvector with hybrid search and text-embedding-3-small | Accepted |
| [0005](0005-redis-streams-for-call-events.md) | Redis Streams for call events, pub/sub for signals | Accepted |
| [0006](0006-flow-as-json-graph.md) | Flow as a JSON graph in JSONB, immutable versions, JSONLogic | Accepted |
| [0007](0007-deepeval-plus-deterministic-checks.md) | DeepEval plus deterministic checks | Accepted |
| [0008](0008-railway-demo-vps-compose.md) | Demo on Railway, production variant on a VPS | Accepted |
| [0009](0009-llm-model-choice.md) | Primary and fallback LLM chosen by evals | Proposed |
| [0010](0010-demo-access-codes-and-signed-tickets.md) | Demo access codes plus signed WebSocket tickets | Accepted |
| [0011](0011-engine-as-shared-package.md) | Dialogue engine as a shared package | Accepted |
| [0012](0012-single-draft-immutable-versions.md) | One in-place draft, versions on publish, rollback copies | Accepted |
| [0013](0013-two-phase-confirmation-in-engine.md) | Two-phase confirmation enforced in engine code | Accepted, amended by 0015 |
| [0014](0014-admin-sessions-in-redis.md) | Admin sessions as an opaque cookie backed by Redis | Accepted |
| [0015](0015-consent-checked-in-code.md) | Consent to a quoted price also checked in code | Accepted |
