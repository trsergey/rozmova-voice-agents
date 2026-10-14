# ADR 0006: Flow as a JSON graph in JSONB, immutable published versions, JSONLogic conditions

- Status: Accepted
- Date: 2026-10-14

## Context

The visual editor and the gateway must read exactly the same flow. Published versions
must never change under a live call, versions need a diff view (FR-12), and admins need to
find which versions use a given tool (FR-18).

## Decision

A flow is a `FlowGraph` JSON document (nodes with typed `data`, edges with optional
`when.route`) defined by Pydantic models in `packages/contracts`. It is stored in JSONB:
`bots.draft_flow` for the draft and `bot_versions.flow` for immutable published versions.
Canvas positions live in `nodes[].ui` and are ignored by validation and diff. `condition`
nodes express branches as JSONLogic over session slots. A GIN `jsonb_path_ops` index
supports usage queries.

## Alternatives considered

- **Normalised node and edge tables.** Easier relational queries, but every save and
  publish becomes a multi-table transaction and the editor needs an ORM mapping.
- **Python expressions for conditions.** Expressive, but evaluating author-written code
  is a security risk and is not portable to the TS editor.

## Consequences

- One model, generated JSON Schema and TS types, no drift between editor and engine.
- Schema evolution of stored JSON needs care (versioned migrations of documents).
- Diff is computed in Python over nodes and edges, excluding `ui`.
