import json
from pathlib import Path
from typing import Any

import pytest

from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import FlowIssue, ValidationResult, validate_flow

DEMO_FLOW = (
    Path(__file__).resolve().parents[2] / "contracts" / "tests" / "fixtures" / "demo_flow.json"
)
DEMO_TOOLS = {"get_balance", "get_current_tariff", "list_services", "activate_service"}


def demo() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DEMO_FLOW.read_text(encoding="utf-8"))
    return data


def node(data: dict[str, Any], node_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(n for n in data["nodes"] if n["id"] == node_id)
    return found


def run(data: dict[str, Any], tools: set[str] = DEMO_TOOLS) -> ValidationResult:
    return validate_flow(FlowGraph.model_validate(data), tools)


def codes(issues: list[FlowIssue]) -> set[tuple[str | None, str]]:
    return {(i.node_id, i.code) for i in issues}


def test_demo_flow_is_valid_without_warnings() -> None:
    result = run(demo())
    assert result.ok
    assert result.errors == []
    assert result.warnings == []


def test_result_is_json_serializable() -> None:
    data = demo()
    data["nodes"].append({"id": "lost", "type": "end", "data": {"farewell": ""}})
    dumped = run(data).model_dump(mode="json")
    assert json.loads(json.dumps(dumped)) == dumped
    assert dumped["warnings"] == []
    assert {"node_id": "lost", "code": "unreachable_node"} in [
        {k: e[k] for k in ("node_id", "code")} for e in dumped["errors"]
    ]
    assert set(dumped["errors"][0]) == {"node_id", "code", "message"}


def test_empty_flow_has_no_start() -> None:
    result = run({"nodes": [], "edges": []})
    assert codes(result.errors) == {(None, "no_start")}


def test_second_start_is_reported_on_that_node() -> None:
    data = demo()
    data["nodes"].append({"id": "start2", "type": "start", "data": {"greeting": "Привіт"}})
    data["edges"].append({"source": "start2", "target": "assist"})
    assert codes(run(data).errors) == {("start2", "multiple_starts")}


def test_duplicate_node_id() -> None:
    data = demo()
    data["nodes"].append({"id": "bye", "type": "end", "data": {"farewell": "Бувайте"}})
    assert codes(run(data).errors) == {("bye", "duplicate_node_id")}


def test_dangling_edges() -> None:
    data = demo()
    data["edges"].append({"source": "tariff_kb", "target": "ghost"})
    data["edges"].append({"source": "ghost", "target": "bye"})
    assert codes(run(data).errors) == {
        ("tariff_kb", "dangling_edge"),
        (None, "dangling_edge"),
    }


def test_no_exit_reachable_and_unreachable_nodes() -> None:
    data = demo()
    data["edges"] = [
        e for e in data["edges"] if e["target"] not in {"bye", "promo"} and e["source"] != "promo"
    ]
    node(data, "assist")["data"]["routes"] = ["tariff"]
    node(data, "promo")["data"]["routes"] = []
    assert codes(run(data).errors) == {
        ("start", "no_exit_reachable"),
        ("promo", "unreachable_node"),
        ("operator", "unreachable_node"),
        ("bye", "unreachable_node"),
    }


def test_exit_reachable_through_any_route_is_enough() -> None:
    data = demo()
    data["nodes"] = [n for n in data["nodes"] if n["id"] not in {"promo", "operator"}]
    data["edges"] = [e for e in data["edges"] if "promo" not in {e["source"], e["target"]}]
    node(data, "assist")["data"]["routes"] = ["tariff", "bye"]
    assert run(data).ok


def test_route_without_edge() -> None:
    data = demo()
    data["edges"] = [e for e in data["edges"] if e.get("when") != {"route": "bye"}]
    assert codes(run(data).errors) == {
        ("assist", "missing_route_edge"),
        ("bye", "unreachable_node"),
    }


def test_duplicate_and_unknown_route_edges() -> None:
    data = demo()
    data["edges"].append({"source": "assist", "target": "operator", "when": {"route": "bye"}})
    data["edges"].append({"source": "assist", "target": "operator", "when": {"route": "help"}})
    data["edges"].append({"source": "tariff_kb", "target": "bye", "when": {"route": "x"}})
    assert codes(run(data).errors) == {
        ("assist", "duplicate_route_edge"),
        ("assist", "unknown_route_edge"),
        ("tariff_kb", "unknown_route_edge"),
    }


def test_waiting_node_may_have_one_default_edge() -> None:
    data = demo()
    data["edges"].append({"source": "assist", "target": "bye"})
    assert run(data).ok
    data["edges"].append({"source": "assist", "target": "tariff_kb"})
    assert codes(run(data).errors) == {("assist", "multiple_default_edges")}


def test_pass_through_nodes_need_a_default_edge() -> None:
    data = demo()
    data["edges"] = [e for e in data["edges"] if e["source"] != "start"]
    data["edges"].append({"source": "start", "target": "assist", "when": {"route": "go"}})
    errors = codes(run(data).errors)
    assert ("start", "missing_default_edge") in errors
    assert ("start", "unknown_route_edge") in errors


def test_terminal_nodes_have_no_edges() -> None:
    data = demo()
    data["edges"].append({"source": "bye", "target": "assist"})
    assert codes(run(data).errors) == {("bye", "edge_from_terminal")}


def condition_flow(cases: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {"greeting": "Вітаю!"}},
            {
                "id": "lookup",
                "type": "tool",
                "data": {"tool": "get_balance", "args": {"msisdn": "{{msisdn}}"}, "save_as": "bal"},
            },
            {"id": "check", "type": "condition", "data": {"cases": cases}},
            {"id": "low", "type": "handoff", "data": {"reason": "low_balance"}},
            {"id": "bye", "type": "end", "data": {"farewell": "До побачення!"}},
        ],
        "edges": [
            {"source": "start", "target": "lookup"},
            {"source": "lookup", "target": "check"},
            *edges,
        ],
    }


LOW = {"when": {"<": [{"var": "bal.amount"}, 10]}, "route": "low"}


def test_condition_routes_and_default_edge() -> None:
    good = condition_flow(
        [LOW],
        [
            {"source": "check", "target": "low", "when": {"route": "low"}},
            {"source": "check", "target": "bye"},
        ],
    )
    assert run(good).ok

    no_route_edge = condition_flow([LOW], [{"source": "check", "target": "bye"}])
    assert codes(run(no_route_edge).errors) == {
        ("check", "missing_route_edge"),
        ("low", "unreachable_node"),
    }

    no_default = condition_flow(
        [LOW], [{"source": "check", "target": "low", "when": {"route": "low"}}]
    )
    assert codes(run(no_default).errors) == {
        ("check", "missing_default_edge"),
        ("bye", "unreachable_node"),
    }


@pytest.mark.parametrize(
    ("node_id", "field", "value"),
    [
        ("start", "greeting", "  "),
        ("assist", "prompt", ""),
        ("tariff_kb", "not_found_reply", "\n"),
        ("operator", "reason", ""),
        ("bye", "farewell", " "),
    ],
)
def test_required_fields(node_id: str, field: str, value: str) -> None:
    data = demo()
    node(data, node_id)["data"][field] = value
    result = run(data)
    assert codes(result.errors) == {(node_id, "empty_field")}
    assert f"'{field}'" in result.errors[0].message


def test_route_names_must_be_filled_and_unique() -> None:
    data = demo()
    node(data, "assist")["data"]["routes"] = ["tariff", "operator", "bye", "bye", ""]
    assert codes(run(data).errors) == {("assist", "duplicate_route"), ("assist", "empty_field")}


def test_unknown_tools() -> None:
    data = demo()
    node(data, "assist")["data"]["tools"].append("teleport")
    result = run(data)
    assert codes(result.errors) == {("assist", "unknown_tool")}
    assert "teleport" in result.errors[0].message

    flow = condition_flow([], [{"source": "check", "target": "bye"}])
    flow["nodes"] = [n for n in flow["nodes"] if n["id"] != "low"]
    assert run(flow).ok
    assert codes(run(flow, tools=set()).errors) == {("lookup", "unknown_tool")}


def test_route_into_llm_that_routes_to_llm_warns() -> None:
    data = demo()
    # assist -> route -> promo -> route -> tariff_kb: two empty LLM calls before the answer.
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "operator"]
    data["edges"] = [e for e in data["edges"] if e["source"] != "promo"]
    data["edges"].append({"source": "promo", "target": "tariff_kb", "when": {"route": "done"}})
    result = run(data)
    assert result.ok
    assert codes(result.warnings) == {("promo", "route_chain")}
    assert "'assist'" in result.warnings[0].message


def test_route_into_llm_with_tools_warns() -> None:
    data = demo()
    node(data, "promo")["data"]["tools"] = ["list_services"]
    result = run(data)
    assert result.ok
    assert codes(result.warnings) == {("promo", "route_chain")}


def test_route_chain_is_followed_through_pass_through_nodes() -> None:
    data = demo()
    data["nodes"].append(
        {"id": "lookup", "type": "tool", "data": {"tool": "get_balance", "save_as": "bal"}}
    )
    data["edges"] = [e for e in data["edges"] if e.get("when") != {"route": "operator"}]
    data["edges"].append({"source": "assist", "target": "lookup", "when": {"route": "operator"}})
    data["edges"].append({"source": "lookup", "target": "promo"})
    # promo routes back to assist: a second route into an LLM node in the same turn.
    data["edges"] = [e for e in data["edges"] if e["source"] != "promo"]
    data["edges"].append({"source": "promo", "target": "assist", "when": {"route": "done"}})
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "operator"]
    result = run(data)
    assert result.ok
    assert codes(result.warnings) == {("promo", "route_chain"), ("assist", "route_chain")}
