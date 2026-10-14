# ADR 0012: One in-place draft per bot, versions created on publish, rollback copies

- Status: Accepted
- Date: 2026-10-14

## Context

Bot authors edit a flow many times before it is ready. Live calls must keep the version
they started with. History must be auditable, and rollback must be fast and safe (FR-7,
FR-8).

## Decision

Each bot has one mutable draft (`bots.draft_flow`) saved in place. Publishing validates
the draft, requires a green eval report for the current draft hash, and creates an
immutable `bot_versions` row with an increasing number; `bots.live_version_id` points to
it and `bot:published` signals the gateway. Rollback creates a new version that copies the
old flow (`note: "rollback of v3"`). A call keeps its version until it ends.

## Alternatives considered

- **A version on every save.** History fills with noise and "publish" needs a separate
  concept anyway.
- **Rollback by moving the live pointer back.** Simpler, but history no longer shows when
  and why the live flow changed.

## Consequences

- Linear, append-only version history; every live change is a row.
- No concurrent editing of drafts (last write wins); acceptable for a small team.
