import copy
import json
from pathlib import Path
from typing import Any

from rozmova_contracts.flow import FlowGraph
from rozmova_engine.flow import EdgeDiff, FieldChange, FlowDiff, NodeDiff, diff_flows

DEMO_FLOW = Path(__file__).resolve().parents[3] / "flows" / "hvylia-demo.json"


def demo() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DEMO_FLOW.read_text(encoding="utf-8"))
    return data


def node(data: dict[str, Any], node_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(n for n in data["nodes"] if n["id"] == node_id)
    return found


def diff(before: dict[str, Any], after: dict[str, Any]) -> FlowDiff:
    return diff_flows(FlowGraph.model_validate(before), FlowGraph.model_validate(after))


def test_same_flow_is_empty() -> None:
    assert diff(demo(), demo()).empty


def test_canvas_positions_and_order_are_not_changes() -> None:
    after = demo()
    for i, n in enumerate(after["nodes"]):
        n["ui"] = {"x": 100.0 * i, "y": -50.0}
    after["nodes"].reverse()
    after["edges"].reverse()

    assert diff(demo(), after) == FlowDiff()


def test_changed_fields_are_listed_with_paths() -> None:
    after = demo()
    node(after, "start")["data"]["greeting"] = "Привіт!"
    assist = node(after, "assist")
    assist["data"]["tools"] = ["get_balance"]

    result = diff(demo(), after)

    assert result.edges == []
    assert result.nodes == [
        NodeDiff(
            id="start",
            type="start",
            status="changed",
            changes=[
                FieldChange(
                    path="data.greeting",
                    before=node(demo(), "start")["data"]["greeting"],
                    after="Привіт!",
                )
            ],
        ),
        NodeDiff(
            id="assist",
            type="llm_prompt",
            status="changed",
            changes=[
                FieldChange(
                    path="data.tools",
                    before=node(demo(), "assist")["data"]["tools"],
                    after=["get_balance"],
                )
            ],
        ),
    ]


def test_a_node_that_changes_type_lists_the_type_and_its_data() -> None:
    after = demo()
    bye = node(after, "bye")
    bye["type"] = "handoff"
    bye["data"] = {"reason": "bye"}

    [change] = diff(demo(), after).nodes

    assert change.status == "changed"
    assert change.type == "handoff"
    assert [c.path for c in change.changes] == ["type", "data.farewell", "data.reason"]
    assert change.changes[1].after is None
    assert change.changes[2].before is None


def test_added_and_removed_nodes_and_edges() -> None:
    """The S4 change from the PRD in reverse and forward: the promo step before handoff."""
    without_promo = demo()
    without_promo["nodes"] = [n for n in without_promo["nodes"] if n["id"] != "promo"]
    without_promo["edges"] = [
        e for e in without_promo["edges"] if e["source"] != "promo" and e["target"] != "promo"
    ]
    without_promo["edges"].append(
        {"source": "assist", "target": "operator", "when": {"route": "operator"}}
    )

    added = diff(without_promo, demo())

    assert added.nodes == [NodeDiff(id="promo", type="llm_prompt", status="added")]
    assert added.edges == [
        EdgeDiff(
            source="assist",
            route="operator",
            status="changed",
            target_before="operator",
            target_after="promo",
        ),
        EdgeDiff(source="promo", route="done", status="added", target_after="operator"),
    ]

    removed = diff(demo(), without_promo)

    assert removed.nodes == [NodeDiff(id="promo", type="llm_prompt", status="removed")]
    assert [(e.source, e.status) for e in removed.edges] == [
        ("assist", "changed"),
        ("promo", "removed"),
    ]


def test_unconditional_edge_has_no_route() -> None:
    after = demo()
    after["edges"] = [e for e in after["edges"] if e["source"] != "start"]

    assert diff(demo(), after).edges == [
        EdgeDiff(source="start", route=None, status="removed", target_before="assist")
    ]


def test_two_edges_on_one_outlet_in_a_draft() -> None:
    after = demo()
    after["edges"].append({"source": "start", "target": "bye"})

    assert diff(demo(), after).edges == [
        EdgeDiff(source="start", route=None, status="added", target_after="bye")
    ]

    before = copy.deepcopy(after)
    after["edges"] = [e for e in after["edges"] if e["source"] != "start"]
    after["edges"].append({"source": "start", "target": "operator"})

    assert diff(before, after).edges == [
        EdgeDiff(source="start", route=None, status="removed", target_before="assist"),
        EdgeDiff(source="start", route=None, status="removed", target_before="bye"),
        EdgeDiff(source="start", route=None, status="added", target_after="operator"),
    ]
