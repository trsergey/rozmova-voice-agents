import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rozmova_contracts.flow import (
    ConditionNode,
    EndNode,
    FlowGraph,
    FlowNode,
    HandoffNode,
    KBAnswerData,
    KBAnswerNode,
    LLMPromptNode,
    StartNode,
    ToolNode,
)

FIXTURES = Path(__file__).parent / "fixtures"
SDD = Path(__file__).resolve().parents[3] / "docs" / "Rozmova_SDD.md"

NODE_SAMPLES: list[tuple[dict[str, Any], type[FlowNode]]] = [
    ({"id": "s", "type": "start", "data": {"greeting": "Вітаю!"}}, StartNode),
    (
        {
            "id": "a",
            "type": "llm_prompt",
            "data": {"prompt": "Ти асистент.", "tools": ["get_balance"], "routes": ["bye"]},
            "ui": {"x": 120, "y": -40.5},
        },
        LLMPromptNode,
    ),
    (
        {
            "id": "t",
            "type": "tool",
            "data": {"tool": "get_balance", "args": {"msisdn": "{{msisdn}}"}, "save_as": "bal"},
        },
        ToolNode,
    ),
    ({"id": "k", "type": "kb_answer", "data": {"not_found_reply": "Не знаю."}}, KBAnswerNode),
    (
        {
            "id": "c",
            "type": "condition",
            "data": {"cases": [{"when": {"<": [{"var": "bal.amount"}, 10]}, "route": "low"}]},
        },
        ConditionNode,
    ),
    ({"id": "h", "type": "handoff", "data": {"reason": "requested"}}, HandoffNode),
    ({"id": "e", "type": "end", "data": {"farewell": "До побачення!"}}, EndNode),
]


def load_demo() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "demo_flow.json").read_text(encoding="utf-8"))
    return data


def graph(*nodes: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> FlowGraph:
    return FlowGraph.model_validate({"nodes": list(nodes), "edges": edges or []})


def test_demo_flow_fixture_matches_sdd() -> None:
    text = SDD.read_text(encoding="utf-8")
    match = re.search(r"\*\*Демо-флоу Hvylia Mobile\*\*.*?```json\n(.*?)```", text, re.DOTALL)
    assert match, "demo flow block not found in the SDD"
    assert json.loads(match.group(1)) == load_demo()


def test_demo_flow_validates_and_round_trips() -> None:
    raw = load_demo()
    flow = FlowGraph.model_validate(raw)

    assert [n.type for n in flow.nodes] == [
        "start",
        "llm_prompt",
        "kb_answer",
        "llm_prompt",
        "handoff",
        "end",
    ]
    routed = {(e.source, e.when.route) for e in flow.edges if e.when}
    assert routed == {
        ("assist", "tariff"),
        ("assist", "operator"),
        ("assist", "bye"),
        ("promo", "done"),
    }
    assert FlowGraph.model_validate_json(flow.model_dump_json()) == flow
    assert flow.model_dump(mode="json", exclude_unset=True) == raw


@pytest.mark.parametrize(("sample", "model"), NODE_SAMPLES)
def test_each_node_type_validates(sample: dict[str, Any], model: type[FlowNode]) -> None:
    node = graph(sample).nodes[0]
    assert isinstance(node, model)
    assert node.model_dump(mode="json", exclude_unset=True) == sample


def test_all_seven_node_types_are_covered() -> None:
    assert {s["type"] for s, _ in NODE_SAMPLES} == {
        "start",
        "llm_prompt",
        "tool",
        "kb_answer",
        "condition",
        "handoff",
        "end",
    }


def test_defaults() -> None:
    kb = KBAnswerData(not_found_reply="Не знаю.")
    assert (kb.top_k, kb.min_similarity) == (5, 0.35)
    node = graph({"id": "a", "type": "llm_prompt", "data": {"prompt": "p"}}).nodes[0]
    assert isinstance(node, LLMPromptNode)
    assert (node.data.tools, node.data.routes, node.ui) == ([], [], None)


@pytest.mark.parametrize(
    "node",
    [
        {"id": "x", "type": "webhook", "data": {}},  # unknown type
        {"id": "x", "type": "start", "data": {}},  # missing greeting
        {"id": "x", "type": "start", "data": {"greeting": "hi", "extra": 1}},
        {"id": "x", "type": "start", "data": {"greeting": "hi"}, "position": {"x": 0, "y": 0}},
        {"id": "x", "type": "end", "data": {"greeting": "hi"}},  # data of another type
        {"id": "x", "type": "kb_answer", "data": {"top_k": 0, "not_found_reply": "-"}},
        {"id": "x", "type": "kb_answer", "data": {"min_similarity": 1.5, "not_found_reply": "-"}},
        {"id": "x", "type": "condition", "data": {"cases": [{"route": "r"}]}},  # no when
        {"id": "x", "type": "start", "data": {"greeting": "hi"}, "ui": {"x": 1}},
    ],
)
def test_malformed_nodes_are_rejected(node: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        graph(node)


def test_malformed_edges_are_rejected() -> None:
    with pytest.raises(ValidationError):
        graph(edges=[{"source": "a", "target": "b", "when": {"name": "x"}}])
    with pytest.raises(ValidationError):
        graph(edges=[{"source": "a"}])


def test_incomplete_draft_is_accepted() -> None:
    # Semantic problems are reported by flow validation (FR-9) with node_id, not here.
    flow = graph(
        {"id": "a", "type": "llm_prompt", "data": {"prompt": "", "routes": ["tariff"]}},
        {"id": "a", "type": "end", "data": {"farewell": ""}},
        edges=[{"source": "a", "target": "missing", "when": {"route": "unknown"}}],
    )
    assert len(flow.nodes) == 2


def test_logic_dump_excludes_ui() -> None:
    flow = graph(NODE_SAMPLES[1][0])
    assert "ui" in flow.model_dump()["nodes"][0]
    assert "ui" not in flow.logic_dump()["nodes"][0]


def test_same_logic_ignores_ui_and_order() -> None:
    raw = load_demo()
    flow = FlowGraph.model_validate(raw)
    moved = {
        "nodes": [{**n, "ui": {"x": i * 100, "y": 50}} for i, n in enumerate(raw["nodes"])][::-1],
        "edges": raw["edges"][::-1],
    }
    assert flow.same_logic(FlowGraph.model_validate(moved))


def test_same_logic_detects_rewired_route() -> None:
    raw = load_demo()
    # The S4 edit: the `operator` route goes straight to handoff instead of `promo`.
    edges = [
        {**e, "target": "operator"} if e.get("when") == {"route": "operator"} else e
        for e in raw["edges"]
    ]
    changed = FlowGraph.model_validate({**raw, "edges": edges})
    assert not FlowGraph.model_validate(raw).same_logic(changed)


def test_logic_hash_follows_same_logic() -> None:
    raw = load_demo()
    flow = FlowGraph.model_validate(raw)
    moved = FlowGraph.model_validate(
        {
            "nodes": [{**n, "ui": {"x": i * 7, "y": 1}} for i, n in enumerate(raw["nodes"])][::-1],
            "edges": raw["edges"][::-1],
        }
    )
    rewired = FlowGraph.model_validate({**raw, "edges": raw["edges"][1:]})

    assert len(flow.logic_hash()) == 64
    assert flow.logic_hash() == moved.logic_hash()
    assert flow.logic_hash() != rewired.logic_hash()
    assert FlowGraph().logic_hash() != flow.logic_hash()
