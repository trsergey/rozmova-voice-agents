# ADR 0014: Admin sessions as an opaque cookie backed by Redis

- Status: Accepted
- Date: 2026-10-15

## Context

The admin SPA is served by admin-api from the same domain (SDD "Admin API і адмінка"), so a
cookie works with no CORS setup. The SDD asks for an httpOnly `SameSite=Lax` session cookie,
a CSRF token on mutating requests, and two roles: `admin` edits, `viewer` only looks. Viewers
also log in with access codes (ROZ-31), and an access code can be revoked, which must end
the sessions it started. Logout has to end a session at once, not when a token expires.
Redis is already a hard dependency of both services.

## Decision

- The cookie `rozmova_session` holds a random 256-bit id and nothing else: httpOnly,
  `SameSite=Lax`, `Secure` in every deployed environment, no `Max-Age` (browser session).
- Redis keeps `admin-session:{sha256(id)}` → JSON `{user_id, email, role, csrf_token}`
  with a sliding TTL (`ROZMOVA_ADMIN_SESSION_TTL_SECONDS`, 12 h): every request extends it
  with `GETEX`. Logout deletes the key. The key is a digest, so a Redis dump holds no
  usable cookies.
- CSRF: a per-session synchronizer token, returned by `POST /auth/login` and `GET /auth/me`
  and required in `X-CSRF-Token` on every non-GET request that uses the session.
- Passwords: `hashlib.scrypt` (n=2^15, r=8, p=1), parameters stored in the hash string;
  checked in a worker thread. An unknown email is checked against a dummy hash.
- The admin user is created or reset on every start from `ROZMOVA_ADMIN_EMAIL` /
  `ROZMOVA_ADMIN_PASSWORD`.

## Alternatives considered

- **JWT (or a signed cookie) with no server state.** No Redis read per request, but logout
  and revoking an access code would need a deny-list in Redis anyway, which brings back the
  state and adds a signing key. One Redis `GETEX` per admin request is cheap.
- **Double-submit CSRF cookie.** Stateless, but the token is readable by any subdomain that
  can set cookies; the token in the session is just as simple once the session is in Redis.
- **Argon2 / bcrypt.** Equally fine; scrypt is in the standard library, so no native
  dependency in the image.
- **Sessions in PostgreSQL.** Durable across a Redis flush, but adds a table and a write per
  request for data that is fine to lose: a lost session means logging in again.

## Consequences

- Flushing Redis logs everyone out; acceptable for an admin tool.
- ROZ-31 (access codes) creates the same session with role `viewer`; revoking a code must
  also delete its sessions, so it will need an index from code to session keys.
- Endpoints that change data depend on `require_admin`; read endpoints on `CurrentSession`.
- No rate limit on `/auth/login` yet: one admin with a long password on the demo. Revisit
  before the URL is shared widely.
