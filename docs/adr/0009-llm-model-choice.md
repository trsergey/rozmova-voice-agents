# ADR 0009: Primary and fallback LLM chosen by evals

- Status: Accepted
- Date: 2026-10-21 (proposed 2026-10-14)

## Context

The model must give a fast first token (no reasoning), reliable function calling,
acceptable Ukrainian and low cost; the p95 end-to-end TTFT target is 1.5 s (PRD NFR).

Where the time goes was measured first, on 51 LLM turns of real calls on Railway dev
(service in `iad`, gpt-4.1-mini, 2026-10-21):

| Turn kind | Turns | TTFT p50 | TTFT p95 |
| --- | --- | --- | --- |
| plain LLM reply | 22 | 1.0 s | 1.9 s |
| reply after a tool call | 22 | 1.6 s | 2.5 s |
| knowledge-base answer | 7 | 2.5 s | 6.9 s |

Gateway and engine add a few milliseconds. The first token of the LLM is the cost: a tool
turn waits for two LLM calls (the call, then the reply), and a KB turn adds ~0.7 s of
search (the query embedding) and a longer prompt.

Candidates were re-checked on 2026-10-21 against the models the OpenAI project can use
and the Anthropic model list (USD per 1M tokens, input / output): gpt-4.1-mini
$0.40 / $1.60, gpt-5.4-mini $0.75 / $4.50, gpt-5.6-luna $0.20 / $1.20, gpt-6-luna
$0.10 / $0.50, claude-haiku-5-5 $0.10 / $0.50. gpt-6-luna reasons by default (15
reasoning tokens on «привіт»), so the OpenAI adapter now sends `reasoning_effort: "none"`
to `gpt-5*` and `gpt-6*` models, as the Anthropic adapter already disables thinking.

## Measurements

The full eval set (52 cases, `flows/hvylia-demo.json`), two runs per model, one case at a
time (`--concurrency 1`), from a laptop in Kyiv. TTFT is end to end over turns that called
the LLM (`summary.ttft_p50_ms` / `ttft_p95_ms` in the report), the mean of the two runs.
Cost is the bot's tokens only, per 100 LLM turns. The three models that passed both runs
got a third, judged run (`gpt-4.1-mini` judge); its result is the third "cases passed"
number and the judge columns.

| Model | Cases passed | TTFT p50 | TTFT p95 | Tool correctness | Faithfulness | Relevancy | $ / 100 turns |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **gpt-4.1-mini** | 52 + 52 + 52 | **1.57 s** | **2.05 s** | 1.000 | 1.00 | 0.78 | $0.080 |
| gpt-5.4-mini | 52 + 51 + 50 | 1.82 s | 2.57 s | 0.98–1.00 | 1.00 | 0.86 | $0.172 |
| gpt-5.6-luna | 52 + 52 + 52 | 2.08 s | 3.14 s | 1.000 | 0.94 | 0.82 | $0.045 |
| gpt-6-luna | 49 + 50 | 1.90 s | 2.78 s | 0.992 | — | — | $0.023 |
| claude-haiku-5-5 | 51 + 51 | 1.15 s | 2.47 s | 1.000 | — | — | $0.036 |

- gpt-6-luna handed off two FAQ and out-of-scope questions it should have answered, and
  once offered `activate_service` for a data package nobody asked for (the code-level
  confirmation of ADR 0013/0015 stopped it).
- gpt-5.4-mini handed off `faq_lost_sim` in two runs, and in the judged run answered the
  fake «1 грн» price rule (`inj_fake_price_override`) by offering an unrelated data
  package: the same unasked `activate_service` as gpt-6-luna.
- claude-haiku-5-5 (after ROZ-93; the first runs failed 12 cases with HTTP 400): before
  every `route` it says a lead-in such as «передаю вас до відповідного розділу» and the
  next node then answers, so the subscriber hears a handoff that does not happen. Neither
  the `route` description nor a system rule stopped it (12 of 12 routed turns). Its best
  p50 is partly that lead-in arriving first. It handed off `faq_lost_sim` in both runs.

## Decision

- **Primary: `openai/gpt-4.1-mini`.** Lowest TTFT p95, every case passed in both runs,
  no reasoning to turn off. It stays the default in the seed, the "new bot" form and the
  eval CLI.
- **Fallback: `openai/gpt-5.6-luna`.** The only other model with every case passed in all
  three runs, at about half the cost of the primary; ~1 s slower at p95, acceptable for a
  fallback. There is no automatic failover: the provider and model are per-bot settings,
  so switching is a draft change and a publish, which runs the eval gate.
- `reasoning_effort: "none"` for `gpt-5*` / `gpt-6*` in the OpenAI adapter.

## Alternatives considered

- **gpt-6-luna.** A quarter of the price, but slower and the only model that broke the
  flow's tool and handoff rules.
- **gpt-5.4-mini as the fallback.** Faster than gpt-5.6-luna, but twice the primary's
  cost and it broke the tool and handoff rules in the judged run.
- **claude-haiku-5-5.** Cheap, fast p50 and works since ROZ-93, but not for flows with
  routes: its lead-in before every route promises a handoff and then the next node
  answers. Holding text back until a route is ruled out would fix it at the cost of the
  first token on every routed node. Revisit if a model of another provider is needed as a
  fallback, which the OpenAI-only pair is not.
- **Reasoning models.** The added first-token latency breaks the voice latency budget.
- **Gemini Flash.** Not in the round to keep two provider adapters (ADR 0003).

## Consequences

- No candidate meets the 1.5 s p95 target alone: on dev, tool turns are ~2.5 s at p95
  because they wait for two LLM calls. Closing the gap is not a model choice; the options
  are a short filler phrase before tool calls or a fixed reply template for simple tool
  results. To be decided with the Gate 2 numbers.
- Laptop TTFT includes the Kyiv → US round trip; compare models by the table, not with
  the dev dashboard.
- Model lists change monthly; re-run this comparison (`make eval args="--no-judge
  --concurrency 1 --model …"`, read the TTFT line of the report) before changing the
  default.
