# Rozmova

[![CI](https://github.com/trsergey/rozmova-voice-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/trsergey/rozmova-voice-agents/actions/workflows/ci.yml)

A voice-agent platform for telecom subscriber support. The MVP is a text channel over
WebSocket with a visual flow editor, LLM function calling, RAG over a knowledge base
and evals in CI.

> Work in progress. Design docs (Ukrainian): [PRD](docs/Rozmova_PRD.md), [SDD](docs/Rozmova_SDD.md).

## Layout

```
apps/gateway        FastAPI WebSocket dialogue gateway
apps/admin-api      FastAPI admin API
packages/contracts  Pydantic models for the protocol and flow graph
packages/engine     Flow executor, LLM providers, tools, RAG, eval runner
packages/core       Settings, database, Redis, logging, telemetry
evals/  kb/  infra/  docs/
```

## Development

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv).

```sh
make install   # sync the workspace and install pre-commit hooks
make lint      # ruff + mypy
make test      # pytest across the workspace
```
