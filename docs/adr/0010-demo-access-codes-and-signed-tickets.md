# ADR 0010: Demo access by code plus a signed ticket for the WebSocket

- Status: Accepted
- Date: 2026-10-14

## Context

The public demo spends money on LLM calls and must not be usable as a free proxy to the
provider key. Reviewers from different companies should get access without accounts,
and access must be revocable and attributable.

## Decision

Viewers sign in with an access code (table `access_codes`, one code per recipient,
revocable) and get the `viewer` role; admins use a separate login. To open a call the
client asks the admin API for a ticket: HMAC-signed, containing `user_id`, `bot_id`,
`call_id`, a `debug` flag and a 60 s expiry, single-use via `ticket:{id}` in Redis. The
gateway verifies signature, expiry, single use and `Origin` before `accept()`, otherwise
closes with 4401. Rate limits key on `user_id` from the ticket.

## Alternatives considered

- **Basic Auth proxy in front of the services.** One shared password, no attribution, no
  per-user limits, and the WebSocket endpoint would still need protection.

## Consequences

- The gateway never exposes the LLM key and rejects connections before doing work.
- `calls` show who called; a leaked code can be revoked alone.
- Binding the ticket to `call_id` prevents hijacking a session on reconnect.
