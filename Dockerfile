# syntax=docker/dockerfile:1.7
# Images of the two services, built from the repo root:
#   docker build --target gateway   -t rozmova-gateway .
#   docker build --target admin-api -t rozmova-admin-api .
# Railway and the VPS run the same images (SDD "Розгортання").

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- build: a virtualenv with one app and its workspace packages -------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Third-party dependencies first: this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
COPY apps/gateway/pyproject.toml apps/gateway/
COPY apps/admin-api/pyproject.toml apps/admin-api/
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/core/pyproject.toml packages/core/
COPY packages/engine/pyproject.toml packages/engine/

FROM build AS build-gateway
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package rozmova-gateway
COPY packages packages
COPY apps/gateway apps/gateway
# --no-editable: the workspace packages are installed into the venv, not linked to /src.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package rozmova-gateway

FROM build AS build-admin-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package rozmova-admin-api
COPY packages packages
COPY apps/admin-api apps/admin-api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package rozmova-admin-api

# --- runtime: the venv only, a non-root user ----------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --create-home --uid 10001 app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# PID 1 is python itself (exec form below), so SIGTERM reaches the graceful shutdown.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=2)"]
LABEL org.opencontainers.image.source=https://github.com/trsergey/rozmova-voice-agents

FROM runtime AS gateway
COPY --from=build-gateway /app/.venv /app/.venv
# Until published versions are served (ROZ-51), the gateway runs the demo flow from a file.
COPY flows/hvylia-demo.json /app/flows/hvylia-demo.json
ENV PORT=8001 \
    ROZMOVA_SERVICE_NAME=gateway \
    ROZMOVA_DEMO_FLOW_PATH=/app/flows/hvylia-demo.json
USER app
EXPOSE 8001
CMD ["python", "-m", "gateway"]

FROM runtime AS admin-api
COPY --from=build-admin-api /app/.venv /app/.venv
# Migrations: `alembic -c /app/alembic.ini upgrade head` (Railway pre-deploy command).
COPY packages/core/alembic.ini /app/alembic.ini
# The demo bot admin-api seeds on start (ROZ-54).
COPY flows/hvylia-demo.json /app/flows/hvylia-demo.json
ENV PORT=8000 \
    ROZMOVA_SERVICE_NAME=admin-api \
    ROZMOVA_DEMO_FLOW_PATH=/app/flows/hvylia-demo.json
USER app
EXPOSE 8000
CMD ["python", "-m", "admin_api"]
