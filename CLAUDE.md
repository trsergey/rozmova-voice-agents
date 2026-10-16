# CLAUDE.md

Conventions for anyone (human or coding agent) changing this repository.

## What this is

Rozmova is a voice-agent platform for telecom subscriber support. The MVP is a text channel:
a WebSocket dialogue gateway runs a bot flow (LLM function calling, RAG over a knowledge base,
operator handoff); an admin web app edits flows visually, publishes versions and shows call logs.

Sources of truth, read them before designing anything non-trivial:

- `docs/Rozmova_PRD.md`: requirements (FR-n, NFR, scenarios S1–S5). Ukrainian.
- `docs/Rozmova_SDD.md`: technical design. Ukrainian. Code must follow it.
- `docs/adr/`: decisions and their alternatives. A new or changed decision gets an ADR.

## Language rules

- Code, comments, identifiers, commit messages, PRs, tickets, ADRs and in-repo docs: **English**.
- Knowledge base (`kb/`), bot prompts, greetings, eval cases (`evals/`), anything a subscriber
  reads or hears: **Ukrainian**.
- PRD and SDD stay Ukrainian.

## Layout

```
apps/gateway        gateway        FastAPI WebSocket sessions; thin layer around the engine
apps/admin-api      admin_api      FastAPI: bots, draft, versions, calls, auth, eval jobs
apps/admin-web                     React + Vite SPA, served by admin-api; the demo chat at /demo/ (ROZ-68)
packages/contracts  rozmova_contracts  Pydantic models: WS protocol, FlowGraph → JSON Schema → TS
packages/engine     rozmova_engine     flow executor, LLM providers, tools, RAG, eval runner
packages/core       rozmova_core       settings, db, Redis, logging, telemetry
evals/  kb/  infra/  docs/
```

Dependency direction: `apps → engine → contracts, core`. Packages never import apps.
The engine has no WebSocket or HTTP code: the gateway, the admin-api eval jobs and CI all
drive the same engine (ADR 0011).

## Commands

```sh
make install    # uv sync --all-packages + pre-commit hooks
make lint       # ruff check, ruff format --check, mypy per workspace member
make fmt        # ruff --fix + format
make test       # pytest across the workspace
make schemas    # export JSON Schema / TS types from contracts
make web-check  # admin-web: eslint, tsc, vitest, build (after make web-install; Node 22+)
```

Use `uv run …` for anything Python; never `pip install`. Add dependencies with
`uv add --package <member> <dep>`.

## Python conventions

- Python 3.12, Pydantic v2, async everywhere on I/O paths, full type hints (mypy strict).
- Contracts are the single source of truth: protocol and flow shapes live in
  `packages/contracts` and nowhere else; TS types are generated, never hand-written.
- asyncio rules (the gateway design depends on them, see SDD "Діалоговий гейтвей"):
  - never swallow `asyncio.CancelledError`; catch it only to clean up and re-raise;
  - hold streams and clients in `async with` so cancellation closes them;
  - side-effect tools run under `asyncio.shield()`;
  - bound every wait: `asyncio.timeout(...)` for turns, first token and tool calls;
  - queues are bounded.
- Errors that reach the client are typed `error` events, not dropped connections.
- Logging: structlog JSON with `call_id`, `bot_id`, `turn_id` bound; never log secrets or full
  provider keys.
- Keep it simple: no frameworks around the LLM (ADR 0003), no abstraction without a second user.

## Testing rules

- Every change ships with tests in the member's `tests/` directory.
- Unit tests never call a real LLM or network: use `FakeProvider` with scripted streams.
- Integration tests use testcontainers (PostgreSQL, Redis); mark them `@pytest.mark.integration`.
- Real models only run in evals (`evals/`) and on the demo.
- The SDD "paper walkthrough" table for the demo flow is the executor's test suite.
- `make lint` and `make test` must pass before a commit; `make web-check` too when `apps/admin-web` changed.

## Workflow

- Work is tracked in Linear, team `ROZ`, project "Rozmova MVP". One ticket, one branch, one PR.
- Branch: `roz-<n>-<short-slug>` (e.g. `roz-12-contracts-protocol`).
- Commit and PR titles start with the ticket id: `ROZ-12: WebSocket protocol contracts`.
- Push the ticket branch and open a PR; the author merges after review. Do not push to `main`.
- No AI co-author or attribution lines in commits or PRs.
- Tick met acceptance criteria in the ticket; say which ones changed or were deferred and why.
