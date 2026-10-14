.DEFAULT_GOAL := help
SRC := apps packages
MEMBERS := $(wildcard packages/*) apps/gateway apps/admin-api

.PHONY: help install lint fmt typecheck test schemas

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

schemas: ## Export JSON Schema from contracts to packages/contracts/schema
	uv run python -m rozmova_contracts.export_schemas
