# ADR 0008: Demo on Railway, production variant as Docker Compose on a VPS

- Status: Accepted
- Date: 2026-10-14

## Context

A public demo must be up within days with PostgreSQL + pgvector, Redis, WebSockets and
TLS, on a budget of roughly $10–20 per month, without server administration. The design
should still show how the same system runs on owned infrastructure.

## Decision

The demo runs on Railway: one project with `dev` (from `main`) and `demo` (from tags)
environments; services `gateway`, `admin-api`, PostgreSQL 16 on `pgvector/pgvector:pg16`
and Redis; databases on private networking only. The gateway sets
`RAILWAY_DEPLOYMENT_DRAINING_SECONDS=60` and `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=30`
explicitly. A production variant is `infra/compose.prod.yml` with Caddy and the full
observability stack, using the same images.

## Alternatives considered

- **Hetzner / OVH VPS for the demo.** Cheaper at scale, but TLS, backups and patching are
  on us from day one.
- **Cloud Run.** Request-based scaling and timeouts fit long WebSocket calls poorly.
- **Vercel.** No long-lived WebSocket servers.

## Consequences

- Fast setup; cost bounded by Railway spend limits.
- PostgreSQL major version is pinned to 16 because the data path changes in 18.
- Railway's drain default is undocumented, so we never rely on defaults.
