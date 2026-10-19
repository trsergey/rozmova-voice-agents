# syntax=docker/dockerfile:1.7
# Images of the two services, built from the repo root:
#   docker build --target gateway   -t rozmova-gateway .
#   docker build --target admin-api -t rozmova-admin-api .
# Railway and the VPS run the same images (SDD "Розгортання").

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim

# --- build-web: the admin-web bundle admin-api serves (/admin/, /demo/, /assets/) -----------
FROM ${NODE_IMAGE} AS build-web
WORKDIR /src/apps/admin-web
COPY apps/admin-web/package.json apps/admin-web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
# The generated protocol types the page imports.
COPY packages/contracts/ts /src/packages/contracts/ts
COPY apps/admin-web ./
RUN npm run build

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
# The cl100k_base tokenizer file the KB chunker needs, fetched at build time, not on start.
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken
RUN /app/.venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

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
ENV PORT=8001 \
    ROZMOVA_SERVICE_NAME=gateway
USER app
EXPOSE 8001
CMD ["python", "-m", "gateway"]

FROM runtime AS admin-api
COPY --from=build-admin-api /app/.venv /app/.venv
# Migrations: `alembic -c /app/alembic.ini upgrade head` (Railway pre-deploy command).
COPY packages/core/alembic.ini /app/alembic.ini
# The demo bot admin-api seeds on start (ROZ-54).
COPY flows/hvylia-demo.json /app/flows/hvylia-demo.json
# The knowledge base admin-api ingests on start (ROZ-27) and its tokenizer file.
COPY kb /app/kb
COPY --from=build-admin-api /app/tiktoken /app/tiktoken
COPY --from=build-web /src/apps/admin-web/dist /app/admin-web
ENV PORT=8000 \
    ROZMOVA_ADMIN_WEB_DIR=/app/admin-web \
    ROZMOVA_SERVICE_NAME=admin-api \
    ROZMOVA_DEMO_FLOW_PATH=/app/flows/hvylia-demo.json \
    ROZMOVA_KB_PATH=/app/kb \
    ROZMOVA_KB_INGEST_ON_START=true \
    TIKTOKEN_CACHE_DIR=/app/tiktoken
USER app
EXPOSE 8000
CMD ["python", "-m", "admin_api"]
