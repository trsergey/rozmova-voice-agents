"""Version diff (FR-12): what changed in the logic of a flow, not on the canvas.

Both flows are compared through ``FlowGraph.logic_dump()``, so ``nodes[].ui`` (positions)
never shows up and the order of nodes and edges does not matter. Nodes match by ``id``; a
changed node lists every changed field as a dotted path into the node (``type``,
``data.prompt``, ``data.tools``). Lists are compared as whole values: the admin shows the
old and the new list rather than a diff of a list of tools or cases.

Edges match by their outlet, the source node plus ``when.route`` (``None`` for the
unconditional edge): an outlet that now points elsewhere is one ``changed`` edge with both
targets. A draft may hold two edges on one outlet; the diff then lists the targets that
disappeared as ``removed`` and the new ones as ``added``.
"""

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from rozmova_contracts.flow import FlowGraph

_CONFIG = ConfigDict(extra="forbid", frozen=True)

Status = Literal["added", "removed", "changed"]


class FieldChange(BaseModel):
    model_config = _CONFIG

    path: str
    before: JsonValue = None
    after: JsonValue = None


class NodeDiff(BaseModel):
    model_config = _CONFIG

    id: str
    type: str  # the node type after the change; before it for a removed node
    status: Status
    changes: list[FieldChange] = []  # only for "changed"


class EdgeDiff(BaseModel):
    model_config = _CONFIG

    source: str
    route: str | None  # None: the node's unconditional edge
    status: Status
    target_before: str | None = None
    target_after: str | None = None


class FlowDiff(BaseModel):
    model_config = _CONFIG

    nodes: list[NodeDiff] = []
    edges: list[EdgeDiff] = []

    @property
    def empty(self) -> bool:
        return not self.nodes and not self.edges


def diff_flows(before: FlowGraph, after: FlowGraph) -> FlowDiff:
    """The changes that turn ``before`` into ``after``."""
    a, b = before.logic_dump(), after.logic_dump()
    return FlowDiff(nodes=_node_diffs(a["nodes"], b["nodes"]), edges=_edge_diffs(a, b))


def _node_diffs(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[NodeDiff]:
    old = {node["id"]: node for node in before}
    new = {node["id"]: node for node in after}
    diffs: list[NodeDiff] = []
    for node_id, node in new.items():
        if node_id not in old:
            diffs.append(NodeDiff(id=node_id, type=node["type"], status="added"))
        elif changes := _field_changes(old[node_id], node, ""):
            diffs.append(NodeDiff(id=node_id, type=node["type"], status="changed", changes=changes))
    diffs += [
        NodeDiff(id=node_id, type=node["type"], status="removed")
        for node_id, node in old.items()
        if node_id not in new
    ]
    return diffs


def _field_changes(before: Any, after: Any, path: str) -> list[FieldChange]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[FieldChange] = []
        for key in [*before, *(k for k in after if k not in before)]:
            if key == "id" and not path:
                continue
            changes += _field_changes(
                before.get(key), after.get(key), f"{path}.{key}" if path else key
            )
        return changes
    return [] if before == after else [FieldChange(path=path, before=before, after=after)]


Outlet = tuple[str, str | None]


def _outlets(dump: dict[str, Any]) -> dict[Outlet, set[str]]:
    outlets: dict[Outlet, set[str]] = defaultdict(set)
    for edge in dump["edges"]:
        when = edge.get("when")
        outlets[(edge["source"], when["route"] if when else None)].add(edge["target"])
    return outlets


def _edge_diffs(before: dict[str, Any], after: dict[str, Any]) -> list[EdgeDiff]:
    old, new = _outlets(before), _outlets(after)
    diffs: list[EdgeDiff] = []
    for outlet in sorted(old.keys() | new.keys(), key=lambda o: (o[0], o[1] or "")):
        source, route = outlet
        gone = sorted(old.get(outlet, set()) - new.get(outlet, set()))
        came = sorted(new.get(outlet, set()) - old.get(outlet, set()))
        if len(gone) == 1 and len(came) == 1:
            diffs.append(
                EdgeDiff(
                    source=source,
                    route=route,
                    status="changed",
                    target_before=gone[0],
                    target_after=came[0],
                )
            )
            continue
        diffs += [
            EdgeDiff(source=source, route=route, status="removed", target_before=target)
            for target in gone
        ]
        diffs += [
            EdgeDiff(source=source, route=route, status="added", target_after=target)
            for target in came
        ]
    return diffs
