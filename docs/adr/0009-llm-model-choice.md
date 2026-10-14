# ADR 0009: Primary and fallback LLM chosen by evals

- Status: Proposed
- Date: 2026-10-14

## Context

The model must give a fast first token (no reasoning), reliable function calling,
acceptable Ukrainian and low cost; p95 end-to-end TTFT target is 1.5 s. Candidates as of
2026-10-14 (USD per 1M tokens, input / output): gpt-4.1-mini $0.40 / $1.60,
gpt-5.4-mini $0.75 / $4.50, gpt-6-luna $0.10 / $0.50, claude-haiku-5-5 $0.10 / $0.50.

## Decision

Pending. Start with `gpt-4.1-mini` as the working default. The final primary and
fallback models will be chosen by running the eval set (ROZ-72) on each candidate and
comparing TTFT, tool-call correctness, Ukrainian quality and cost. This ADR will be
updated with the numbers and moved to Accepted.

## Alternatives considered

- **Reasoning models.** Better at complex tasks, but the added first-token latency breaks
  the voice-oriented latency budget.
- **Gemini Flash.** Not in the first round to keep two provider adapters (ADR 0003).

## Consequences

- Model lists change monthly; re-check candidates and prices right before the run.
- The provider and model are per-bot configuration, so switching needs no code change.
