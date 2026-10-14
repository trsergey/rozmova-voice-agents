# ADR 0003: Own LLM provider abstraction, no framework

- Status: Accepted
- Date: 2026-10-14

## Context

Bots choose their provider and model in configuration (FR-16): OpenAI, Azure OpenAI,
OpenRouter or Anthropic. The engine needs streaming text deltas, tool calls and usage
from all of them, with predictable latency and retries.

## Decision

Define a small `LLMProvider` protocol in `packages/engine` whose `stream()` yields common
events (`TextDelta | ToolCall | Usage`). Implement `OpenAICompatibleProvider` (OpenAI,
Azure OpenAI, OpenRouter via config) and `AnthropicProvider` on the official SDKs. Tests
use a `FakeProvider` with scripted streams.

## Alternatives considered

- **LangChain / LlamaIndex.** Broad provider coverage, but extra layers hide streaming and
  cancellation behaviour, add dependency churn and make TTFT harder to reason about.

## Consequences

- We write and maintain two adapters (a few hundred lines).
- Retry policy is ours: exponential backoff on 429/5xx before the first token; no retry
  after it, a `retryable` error instead.
- Adding a provider means one adapter class, no changes outside `providers/`.
