"""The engine's spans and metrics (ROZ-53) on a tool loop and an executor with FakeProvider."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from pydantic import BaseModel

from rozmova_contracts import protocol
from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import FlowExecutor
from rozmova_engine.providers import ToolCall, Usage
from rozmova_engine.providers.fake import FakeProvider, text_turn
from rozmova_engine.session import SessionState
from rozmova_engine.timings import TurnTimer
from rozmova_engine.tool_loop import ToolLoop
from rozmova_engine.tools import Tool, ToolContext, ToolRegistry, ToolResult

FLOW = FlowGraph.model_validate(
    {
        "nodes": [
            {"id": "start", "type": "start", "data": {"greeting": "Вітаю!"}},
            {
                "id": "ask",
                "type": "llm_prompt",
                "data": {"prompt": "Допомагай.", "tools": ["balance"]},
            },
        ],
        "edges": [{"source": "start", "target": "ask"}],
    }
)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Spans recorded during the test. The global provider can be set once per process,
    so an exporter is added to it rather than replacing it."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class NoArgs(BaseModel):
    pass


async def balance(_: NoArgs, __: ToolContext) -> ToolResult:
    return {"balance_uah": 120.5}


async def run_turn(provider: FakeProvider) -> list[protocol.ProtocolMessage]:
    events: list[protocol.ProtocolMessage] = []

    async def emit(event: protocol.ProtocolMessage) -> None:
        events.append(event)

    registry = ToolRegistry([Tool("balance", "", NoArgs, balance)])
    ctx = ToolContext(subscriber_id="demo")
    loop = ToolLoop(provider, "gpt-4.1-mini", registry, ctx, emit, provider_name="openai")
    executor = FlowExecutor(FLOW, loop, registry, ctx, emit)
    state = SessionState(call_id=uuid4(), bot_id=uuid4(), bot_version=1)
    await executor.start(state, TurnTimer())
    timer = TurnTimer()
    await executor.handle(state, "скільки грошей", timer)
    assert timer.first_text_node_type == "llm_prompt"
    return events


def by_name(spans: InMemorySpanExporter) -> dict[str, list[ReadableSpan]]:
    out: dict[str, list[ReadableSpan]] = {}
    for span in spans.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


async def test_a_turn_has_node_llm_and_tool_spans(spans: InMemorySpanExporter) -> None:
    provider = FakeProvider(
        [
            [ToolCall(id="c1", name="balance"), Usage(input_tokens=100, output_tokens=5)],
            text_turn("120,50 грн.", usage=Usage(input_tokens=120, output_tokens=7)),
        ]
    )
    await run_turn(provider)

    named = by_name(spans)
    assert [(s.attributes or {}).get("node.type") for s in named["flow.node"]] == [
        "start",
        "llm_prompt",
    ]
    llm = named["llm.stream"]
    assert len(llm) == 2
    attributes: Any = llm[0].attributes
    assert attributes["llm.provider"] == "openai"
    assert attributes["llm.model"] == "gpt-4.1-mini"
    assert attributes["ttft_ms"] >= 0
    assert (attributes["llm.input_tokens"], attributes["llm.output_tokens"]) == (100, 5)
    [tool] = named["tool.balance"]
    assert tool.attributes is not None
    assert tool.attributes["tool.status"] == "ok"
    # The LLM and tool spans are children of the llm_prompt node's span.
    node = named["flow.node"][1]
    assert node.context is not None
    for child in [*llm, tool]:
        assert child.parent is not None
        assert child.parent.span_id == node.context.span_id


async def test_llm_and_tool_metrics(spans: InMemorySpanExporter) -> None:
    ttft_before = sample("rozmova_llm_ttft_seconds_count", provider="openai", model="gpt-4.1-mini")
    tools_before = sample("rozmova_tool_calls_total", tool="balance", status="ok")
    unknown_before = sample("rozmova_tool_calls_total", tool="unknown", status="error")
    tokens_before = sample("rozmova_llm_tokens_total", provider="openai", direction="output")

    provider = FakeProvider(
        [
            [ToolCall(id="c1", name="balance"), ToolCall(id="c2", name="made_up")],
            text_turn("Готово.", usage=Usage(input_tokens=50, output_tokens=3)),
        ]
    )
    await run_turn(provider)

    assert (
        sample("rozmova_llm_ttft_seconds_count", provider="openai", model="gpt-4.1-mini")
        == ttft_before + 2
    )
    assert sample("rozmova_tool_calls_total", tool="balance", status="ok") == tools_before + 1
    # A tool name the LLM made up does not become a label of its own.
    assert sample("rozmova_tool_calls_total", tool="unknown", status="error") == unknown_before + 1
    assert sample("rozmova_llm_tokens_total", provider="openai", direction="output") == (
        tokens_before + 3
    )
