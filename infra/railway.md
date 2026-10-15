# Railway

The demo runs on Railway (SDD "Розгортання", ADR 0008). This file records how the
project is set up, so it can be rebuilt by hand or checked against the dashboard.

## Project

| | |
| --- | --- |
| Project | `rozmova` (`9f269e26-ac98-4a2d-819d-e26fc1b7b467`), workspace "trsergey's Projects" |
| Environments | `dev` (`69046bb5-0386-42dc-9e3d-157c76c48466`); `demo` comes with the tag release (ROZ-44) |
| Region | `iad` (US East), close to the LLM providers' APIs |
| Plan | Trial (one-time $5 credit, which is also the spend cap). Before the demo is shared: Hobby with a usage limit of $20 per month (Workspace → Usage → Usage limits). |

| Service | Source | Public domain |
| --- | --- | --- |
| `gateway` | `ghcr.io/trsergey/rozmova-gateway:<sha>` | `gateway-dev-9b22.up.railway.app` → port 8001 |
| `admin-api` | `ghcr.io/trsergey/rozmova-admin-api:<sha>` | `admin-api-dev-0e9c.up.railway.app` → port 8000 |
| `Postgres` | `pgvector/pgvector:0.8.1-pg16`, volume at `/var/lib/postgresql/data` | none |
| `Redis` | `redis:8.2` (Railway template), volume at `/data` | none |

PostgreSQL and Redis have no TCP proxy: they are reachable only over the private network.
The images are public in GHCR (they inherit the repository's visibility), so Railway pulls
them without registry credentials.

PostgreSQL comes from Railway's `postgres` template (variables, generated password,
volume), deployed *staged* with the image switched to `pgvector/pgvector:0.8.1-pg16` before
the first start. The template's own image is PG 18 without pgvector, and once PG 18 has
initialised the volume PG 16 cannot start on it.

## Service settings

Both apps: healthcheck path `/healthz` (timeout 60 s), restart on failure (5 retries).
admin-api: pre-deploy command `alembic -c /app/alembic.ini upgrade head`, so every deploy
migrates the database before the new version takes traffic.

## Variables

| Variable | Service | Value | Why |
| --- | --- | --- | --- |
| `PORT` | gateway / admin-api | `8001` / `8000` | Railway injects `PORT=8080` and it overrides the image's `ENV PORT`; the domains target 8001/8000, so without this the proxy gets "connection refused" (502). |
| `ROZMOVA_ENV` | both | `dev` | |
| `ROZMOVA_DATABASE_URL` | both | `postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.POSTGRES_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}` | asyncpg driver, private network. |
| `ROZMOVA_REDIS_URL` | both | `${{Redis.REDIS_URL}}` | Tickets, session state, rate limits. |
| `ROZMOVA_TICKET_SECRET` | both, same value | random 32 bytes hex | Signs the WebSocket tickets (admin-api issues, gateway verifies). Set from stdin so it never appears in a command line or a log: `openssl rand -hex 32 \| tee >(railway variable set ROZMOVA_TICKET_SECRET --stdin -s gateway) \| railway variable set ROZMOVA_TICKET_SECRET --stdin -s admin-api` |
| `ROZMOVA_ADMIN_EMAIL` | admin-api | the admin's login email | admin-api creates this user with role `admin` on start (ROZ-26). |
| `ROZMOVA_ADMIN_PASSWORD` | admin-api | a long random password, set from stdin | Changing it and redeploying resets the password: `openssl rand -base64 24 \| tee /dev/tty \| railway variable set ROZMOVA_ADMIN_PASSWORD --stdin -s admin-api` |
| `ROZMOVA_ALLOWED_ORIGINS` | gateway | `["https://${{admin-api.RAILWAY_PUBLIC_DOMAIN}}"]` | Browser origins allowed to open the WebSocket. |
| `ROZMOVA_DEMO_API_KEY` | gateway | OpenAI key, entered in the dashboard only | The demo bot's LLM key (`gpt-4.1-mini`). Until it is set, LLM turns end with `error{llm_unavailable}`; the greeting does not need it. |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | gateway | `60` | Time between SIGTERM and SIGKILL for the old deployment. The default is 0 s, which would cut every call on deploy. The gateway drains its calls in 50 s (`DRAIN_SECONDS` in `apps/gateway/src/gateway/__main__.py`) and exits 0 before the kill. |
| `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` | gateway | `30` | The new deployment takes traffic while the old one still runs, so clients reconnect to it right after `session_end{shutdown}`. |

## Deploys

CI deploys with `infra/deploy.py` (ROZ-44):

- push to `main` → job `deploy-dev` → environment `dev`;
- tag `v*` → job `release` → environment `demo` (to be created; see below);
- a manual run (`gh workflow run ci.yml --ref <branch>`) deploys that ref to `dev`.

The script switches `gateway` and `admin-api` to `ghcr.io/trsergey/rozmova-<service>:<sha>`
with one environment patch (GraphQL `environmentPatchCommit`, so only that environment
changes), waits until the new deployments are SUCCESS, runs the smoke test below, and on
any failure switches the changed services back to the images they ran before. While a new
deployment fails, Railway keeps the old one serving, so a broken release costs no
downtime.

GitHub environments `dev` / `demo` hold the per-environment settings:

| Name | Kind | Value |
| --- | --- | --- |
| `RAILWAY_TOKEN` | secret | A Railway **project token** for that environment (Project Settings → Tokens). |
| `RAILWAY_ENVIRONMENT` | variable | `dev` / `demo` |
| `GATEWAY_URL`, `ADMIN_URL` | variable | The public HTTPS domains of the two services. |

Before the first `v*` tag: create the `demo` environment on Railway (duplicate `dev`, new
secrets and domains), the GitHub environment `demo` with the values above, and move to the
Hobby plan.

Run it locally (with `railway login` and the project linked):

```sh
uv run python infra/deploy.py --environment dev --sha <commit> \
  --gateway-url https://gateway-dev-9b22.up.railway.app \
  --admin-url https://admin-api-dev-0e9c.up.railway.app
```

## Smoke test

`infra/deploy.py` checks from outside, through Railway's proxy: both `/healthz` return 200
and a WebSocket to the gateway without a ticket is refused with `4401`. That proves TLS,
the upgrade, routing and the app; a full call needs a ticket from the admin API and joins
the smoke test with `POST /calls/ticket` (ROZ-61).

### A full call by hand

Until the admin API issues tickets (`POST /calls/ticket`, ROZ-61), a ticket comes from
inside the gateway container, which can reach the private Redis:

```sh
# Once: an SSH key for Railway (railway ssh keys add --key ~/.ssh/railway_ed25519.pub).
railway ssh -s gateway -e dev -- python -m gateway.devticket
# prints ws://localhost:8001/ws/calls/<bot>?call_id=…&ticket=…
# replace ws://localhost:8001 with wss://gateway-dev-9b22.up.railway.app and connect
```

Expected: `session_started`, the greeting as `assistant_delta`, `turn_end`; `hangup` gives
`session_end{hangup}`; the same ticket a second time is refused with close code `4401`.
Checked on 2026-10-15 with images `d3fac9e`: greeting after ~0.5 s from Kyiv, including
the TLS handshake.
