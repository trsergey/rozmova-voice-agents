# ADR 0016: A filler phrase before tool calls

- Status: Accepted
- Date: 2026-10-21

## Context

ADR 0009 kept `gpt-4.1-mini` and measured why the 1.5 s p95 TTFT target is missed: on dev a
turn that calls a tool reaches ~2.5 s p95 against ~1.9 s for a plain reply. The subscriber
hears nothing until the second LLM call, the one after the tool, streams its first token:
the time of the first call (which only decides on the tool), the tool and the second call's
first token add up. In a voice channel that is dead air; a human operator says «хвилинку»
and looks it up.

## Decision

When an LLM step asks for tools and the subscriber has been sent no text in this turn
(`TurnTimer.spoke`), the engine sends the called tool's filler phrase at once, before the
tools run. Each tool declares its phrase next to its description (`Tool.filler`):
«Хвилинку, перевіряю баланс.», «Хвилинку, дивлюся ваш тариф.», «Хвилинку, дивлюся
послуги.», «Хвилинку.» for `activate_service`, «Хвилинку, шукаю інформацію.» for
`search_knowledge_base`. Rules:

- at most one filler per turn, and none when the model already said something with the
  call (Claude often does) or a fixed node spoke earlier in the turn;
- the filler is the text of the assistant message that carries the tool calls, so the
  model sees it was said and does not open its answer the same way; both providers accept
  text next to tool calls;
- the step's text, `turn_end` and the eval reply are everything said in the turn, filler
  included.

TTFT stays end to end to the first `assistant_delta`, so for tool turns it now measures
the filler: the time until the subscriber hears the bot, which is what the PRD budgets.

## Measurements

Full eval set, `gpt-4.1-mini`, judged run with the default concurrency, laptop in Kyiv:

| | TTFT p50 | TTFT p95 | answer_relevancy | Cases passed |
| --- | --- | --- | --- | --- |
| before (ROZ-72) | 1.57 s | 3.15 s | 0.78 | 52/52 |
| with the filler | 0.95 s | 2.04 s | 0.72 | 51/52 |

The failed case (`oos_phone_advice`, a handoff) calls no tool, so it has no filler; it
passed 3 of 3 reruns with the filler and 3 of 3 without it. The judge scores the extra
sentence as slightly irrelevant; 0.72 is well above the 0.6 threshold.

## Alternatives considered

- **A reply template for simple tool results** (balance, tariff) with no second LLM call.
  Faster for those tools, but every tool needs its own wording, two-phase confirmation and
  multi-tool questions still need the LLM, and the flow editor would have to own the
  templates.
- **The filler in the flow (per node), as the SDD first sketched (`data.filler`).**
  Editable without a release, but needs a contract and editor change, and the phrase
  belongs to the tool, not the node.
- **Asking the LLM to speak before the call.** Not reliable with OpenAI models, which
  call tools without text, and every token of that text is first-call latency.

## Consequences

- Tool turns reach the plain-turn TTFT; the target is still missed by the LLM's own first
  token (~1.9 s p95 on dev). Further cuts are model or region choices (ADR 0009).
- `kb_answer` nodes, the slowest turns (search plus a long prompt), get no filler: they are
  not tool calls. Same mechanism would apply if they need one.
- A new tool should set `filler`; without it the turn is silent until the answer, as
  before.
