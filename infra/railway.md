# Railway

Notes for the Railway project (ROZ-38 creates it). The SDD section "Розгортання" is the
design; this file lists what has to be set by hand.

## Gateway service variables

| Variable | Value | Why |
| --- | --- | --- |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `60` | Time between SIGTERM and SIGKILL for the old deployment. The default is reported as 0 s, which would cut every call on deploy. The gateway drains its calls in 50 s (`DRAIN_SECONDS` in `apps/gateway/src/gateway/__main__.py`) and exits 0 before the kill. |
| `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` | `30` | The new deployment takes traffic while the old one still runs, so clients reconnect to it right after `session_end{shutdown}`. |
| `ROZMOVA_TICKET_SECRET` | random, `openssl rand -hex 32` | Same value as in admin-api: it signs the WebSocket tickets. |
| `ROZMOVA_ALLOWED_ORIGINS` | `["https://<admin-api domain>"]` | Browser origins allowed to open the WebSocket. |
| `ROZMOVA_REDIS_URL` | private Redis URL | Tickets, session state, rate limits. |
