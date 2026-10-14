# ADR 0007: DeepEval for LLM metrics plus deterministic checks for tools and injection

- Status: Accepted
- Date: 2026-10-14

## Context

Quality gates must say "100% of injection cases rejected" and "≥ 95% correct tool calls"
without flakiness, and also judge answer relevancy and faithfulness to RAG sources. Evals
run locally, in CI and in the admin API before publishing (FR-19).

## Decision

Each YAML eval case is checked in two ways. Deterministic checks (no LLM judge): which
tools were called with which arguments, whether handoff happened, that `activate_service`
never ran without confirmation, that injection cases did not trigger tools. LLM-judged
metrics use DeepEval (answer relevancy, faithfulness) with thresholds in
`evals/thresholds.yaml`. The runner lives in `packages/engine` (ADR 0011).

## Alternatives considered

- **Hand-written asserts only.** Cannot judge free-text answer quality.
- **promptfoo.** Strong tooling, but Node-based and outside the Python engine, so evals
  would not run through the same executor as production.

## Consequences

- Hard guarantees come from deterministic checks; judge metrics have margins for noise.
- Each run costs LLM tokens; CI runs a fast subset on relevant changes only.
