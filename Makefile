.DEFAULT_GOAL := help
SRC := apps packages
MEMBERS := $(wildcard packages/*) apps/gateway apps/admin-api

COMPOSE := docker compose -f infra/compose.dev.yml

.PHONY: help install lint fmt typecheck test schemas client migrate ingest eval eval-fast up up-apps down down-v ps logs web-install web-dev web-check

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install all workspace packages and git hooks
	uv sync --all-packages
	uv run pre-commit install

lint: ## Ruff lint, format check and mypy
	uv run ruff check $(SRC)
	uv run ruff format --check $(SRC)
	$(MAKE) typecheck

typecheck: ## Mypy per workspace member (src + tests)
	@set -e; for m in $(MEMBERS); do echo "mypy $$m"; uv run mypy $$m/src $$m/tests; done

fmt: ## Auto-fix lint issues and format
	uv run ruff check --fix $(SRC)
	uv run ruff format $(SRC)

test: ## Run pytest across the workspace
	uv run pytest

CONTRACTS := packages/contracts
JSON2TS := npx --yes json-schema-to-typescript@15.0.4

schemas: ## Export JSON Schema and TS types from contracts
	uv run python -m rozmova_contracts.export_schemas
	@mkdir -p $(CONTRACTS)/ts
	@set -e; for f in $(CONTRACTS)/schema/*.json; do \
		out=$(CONTRACTS)/ts/$$(basename $$f .json).d.ts; \
		$(JSON2TS) --input $$f --output $$out --additionalProperties false; \
		echo "wrote $$out"; \
	done

OPENAPI_TS := npx --yes openapi-typescript@7.13.0

client: ## Export the admin API's OpenAPI schema and generate the admin-web TS client types
	uv run python -m admin_api.openapi
	$(OPENAPI_TS) apps/admin-api/openapi.json --output apps/admin-web/src/api/schema.d.ts

ALEMBIC := uv run alembic -c packages/core/alembic.ini

migrate: ## Apply DB migrations to ROZMOVA_DATABASE_URL (make migrate rev=-1 to step back)
	$(if $(rev),$(ALEMBIC) downgrade $(rev),$(ALEMBIC) upgrade head)

ingest: ## Ingest kb/ into pgvector (needs OPENAI_API_KEY; make ingest args=--fake for offline vectors)
	uv run python -m rozmova_engine.rag.ingest $(args)

EVAL := uv run --package rozmova-engine --extra evals python -m rozmova_engine.evals

eval: ## Run the full eval set with the judge (needs make up + make ingest and OPENAI_API_KEY)
	$(EVAL) $(args)

eval-fast: ## Run the first 2 eval cases of each tag (make eval-fast args=--no-judge to skip the judge)
	$(EVAL) --fast $(args)

up: ## Start the local stack (Postgres, Redis, Jaeger, Prometheus, Grafana) and wait until healthy
	$(COMPOSE) up -d --wait

up-apps: ## Build the service images and run them on top of the local stack
	$(COMPOSE) --profile apps up -d --build --wait

down: ## Stop the local stack, keep data volumes
	$(COMPOSE) --profile apps down

down-v: ## Stop the local stack and delete its data volumes
	$(COMPOSE) --profile apps down -v

ps: ## Show local stack status and health
	$(COMPOSE) ps

logs: ## Follow local stack logs (make logs s=postgres for one service)
	$(COMPOSE) logs -f $(s)

WEB := npm --prefix apps/admin-web

web-install: ## Install the admin-web dependencies (Node 22+)
	$(WEB) ci

web-dev: ## Run admin-web on Vite (localhost:5173/admin/ and /demo/), API proxied to localhost:8000
	$(WEB) run dev

web-check: ## admin-web: eslint, tsc, vitest and the production build
	$(WEB) run lint
	$(WEB) run typecheck
	$(WEB) test
	$(WEB) run build
