"""Runs eval cases through the flow executor, the same engine the gateway drives (ADR 0011).

Each case is one call: a fresh ``SessionState``, a fresh mock billing and the real model,
knowledge base and flow. Every turn records the reply, the tool calls with their results,
the chunks the knowledge base returned and the turn's timings, which is all the checks and
the judge need. Turns after the call has ended (handoff or goodbye) are skipped.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from pydantic import JsonValue

from rozmova_contracts import protocol
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.evals.cases import EvalCase
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.providers import LLMProvider, Usage
from rozmova_engine.rag.answer import KnowledgeBaseAnswerer, KnowledgeSearch, knowledge_tools
from rozmova_engine.rag.search import SearchResult
from rozmova_engine.session import SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import (
    DEMO_SUBSCRIBER_ID,
    MockBilling,
    ToolContext,
    ToolRegistry,
    demo_subscribers,
    subscriber_tools,
)

log = structlog.get_logger(__name__)

# The gateway's turn budget is 30 s; evals allow more, the model is not the point here.
TURN_TIMEOUT_S = 60.0
DEFAULT_CONCURRENCY = 4


@dataclass(slots=True)
class ToolCallRecord:
    name: str
    args: dict[str, JsonValue]
    result: dict[str, JsonValue] | None = None


@dataclass(slots=True)
class TurnRecord:
    user: str
    reply: str = ""
    tools: list[ToolCallRecord] = field(default_factory=list)
    retrieval: list[str] = field(default_factory=list)
    sources: list[protocol.Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timings: protocol.TurnTimings | None = None
    skipped: bool = False


@dataclass(slots=True)
class CaseRun:
    case: EvalCase
    turns: list[TurnRecord] = field(default_factory=list)
    ended: protocol.SessionEndReason | None = None
    activated: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=0, output_tokens=0))
    # Set when the run itself broke (timeout, provider down): the case fails.
    error: str | None = None

    @property
    def handed_off(self) -> bool:
        return self.ended == "handoff"


@dataclass(frozen=True, slots=True)
class Bot:
    """What a run is made of: the flow, the LLM and the knowledge base."""

    flow: FlowGraph
    provider: LLMProvider
    model: str
    kb: KnowledgeSearch


class _RecordingSearch:
    """Passes searches through and keeps the chunk texts for the current turn."""

    def __init__(self, kb: KnowledgeSearch) -> None:
        self._kb = kb
        self.turn: TurnRecord | None = None

    async def search(self, query: str, *, top_k: int, min_similarity: float) -> SearchResult:
        result = await self._kb.search(query, top_k=top_k, min_similarity=min_similarity)
        if self.turn is not None:
            self.turn.retrieval.extend(hit.text for hit in result.hits)
        return result


async def run_case(bot: Bot, case: EvalCase, *, turn_timeout: float = TURN_TIMEOUT_S) -> CaseRun:
    """Play ``case`` as one call; never raises except on cancellation."""
    run = CaseRun(case=case)
    billing = MockBilling(demo_subscribers())
    subscriber = billing.find(DEMO_SUBSCRIBER_ID)
    if subscriber is None:
        raise RuntimeError("the mock billing has no demo subscriber")
    before = set(subscriber.services)
    search = _RecordingSearch(bot.kb)
    registry = ToolRegistry([*subscriber_tools(billing), *knowledge_tools(search)])
    ctx = ToolContext(subscriber_id=DEMO_SUBSCRIBER_ID)
    current = TurnRecord(user="")

    async def emit(event: protocol.ProtocolMessage) -> None:
        match event:
            case protocol.ToolCall(name=name, args=args):
                current.tools.append(ToolCallRecord(name=name, args=dict(args)))
            case protocol.ToolResult(name=name, result=result):
                pending = next(
                    (t for t in reversed(current.tools) if t.name == name and t.result is None),
                    None,
                )
                if pending is not None:
                    pending.result = dict(result) if isinstance(result, dict) else {"value": result}
            case protocol.Error(code=code, message=message):
                current.errors.append(f"{code}: {message}")
            case _:
                pass

    loop = ToolLoop(bot.provider, bot.model, registry, ctx, emit)
    executor = FlowExecutor(
        bot.flow, loop, registry, ctx, emit, kb=KnowledgeBaseAnswerer(search, loop, emit)
    )
    state = SessionState(call_id=case_call_id(case), bot_id=case_call_id(case), bot_version=None)
    try:
        async with asyncio.timeout(turn_timeout):
            await executor.start(state, TurnTimer())
        for turn in case.turns:
            current = TurnRecord(user=turn.user)
            run.turns.append(current)
            if state.current_node is None:
                current.skipped = True
                continue
            search.turn = current
            timer = TurnTimer()
            async with asyncio.timeout(turn_timeout):
                outcome = await executor.handle(state, turn.user, timer)
            current.reply = outcome.text
            current.sources = outcome.sources
            current.timings = timer.to_timings()
            run.usage = _add(run.usage, outcome.usage)
            if outcome.ended is not None:
                run.ended = outcome.ended
    except TimeoutError:
        run.error = f"turn {len(run.turns)} timed out after {turn_timeout:g} s"
    except Exception as exc:  # the report says why; one broken case must not stop the run
        log.exception("eval_case_failed", case_id=case.id)
        run.error = f"{type(exc).__name__}: {exc}"
    run.activated = sorted(subscriber.services - before)
    return run


async def run_cases(
    bot: Bot,
    cases: list[EvalCase],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_done: Callable[[CaseRun], None] | None = None,
) -> list[CaseRun]:
    """Run the cases ``concurrency`` at a time; results keep the order of ``cases``."""
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: EvalCase) -> CaseRun:
        async with semaphore:
            run = await run_case(bot, case)
        if on_done is not None:
            on_done(run)
        return run

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(case), name=f"eval-{case.id}") for case in cases]
    return [task.result() for task in tasks]


def case_call_id(case: EvalCase) -> uuid.UUID:
    """A stable id per case, so logs of two runs line up."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rozmova-eval:{case.id}")


def _add(total: Usage, usage: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + usage.input_tokens,
        output_tokens=total.output_tokens + usage.output_tokens,
    )
