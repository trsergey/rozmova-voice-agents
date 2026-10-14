"""Flow validation before publish (FR-9).

The ``FlowGraph`` contract checks shape only, so a half-finished draft still saves. This
module runs the semantic checks from the SDD section "Флоу": exactly one ``start``, an
exit (``handoff`` or ``end``) reachable from it, no unreachable nodes, an edge for every
route, required texts filled and known tools. Each issue carries the ``node_id`` the admin
highlights; flow-level issues (no ``start`` at all) have ``node_id=None``.

Warnings do not block publishing. The only one is the latency rule from the SDD section
"Бюджет затримки": a turn makes at most one empty LLM call (a route or a tool call) before
the reply starts, so a route into an ``llm_prompt`` that may route on to another LLM node
or call a tool in the same turn is flagged.

The validator is pure and synchronous. The caller passes the names of the registered tools,
so it does not depend on the tool registry.
"""

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rozmova_contracts.flow import (
    ConditionNode,
    Edge,
    EndNode,
    FlowGraph,
    FlowNode,
    HandoffNode,
    KBAnswerNode,
    LLMPromptNode,
    StartNode,
    ToolNode,
)

_CONFIG = ConfigDict(extra="forbid", frozen=True)

ErrorCode = Literal[
    "no_start",
    "multiple_starts",
    "duplicate_node_id",
    "dangling_edge",
    "no_exit_reachable",
    "unreachable_node",
    "missing_route_edge",
    "duplicate_route_edge",
    "unknown_route_edge",
    "missing_default_edge",
    "multiple_default_edges",
    "edge_from_terminal",
    "empty_field",
    "duplicate_route",
    "unknown_tool",
]
WarningCode = Literal["route_chain"]

_ErrorSink = Callable[[str | None, ErrorCode, str], None]


class FlowIssue(BaseModel):
    """One problem found in a flow; ``node_id`` is ``None`` only for flow-level issues."""

    model_config = _CONFIG

    node_id: str | None
    code: ErrorCode | WarningCode
    message: str


class ValidationResult(BaseModel):
    """Errors block publishing; warnings are shown in the editor only."""

    model_config = _CONFIG

    errors: list[FlowIssue] = Field(default_factory=list)
    warnings: list[FlowIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_TERMINAL = (HandoffNode, EndNode)
_PASS_THROUGH = (StartNode, ToolNode, ConditionNode)
"""Nodes executed without an utterance and without the LLM (terminal ones aside)."""


def validate_flow(flow: FlowGraph, known_tools: Collection[str]) -> ValidationResult:
    """Check ``flow`` for publishing; ``known_tools`` are the names in the tool registry."""
    errors: list[FlowIssue] = []

    def error(node_id: str | None, code: ErrorCode, message: str) -> None:
        errors.append(FlowIssue(node_id=node_id, code=code, message=message))

    nodes: dict[str, FlowNode] = {}
    for node in flow.nodes:
        if node.id in nodes:
            error(node.id, "duplicate_node_id", f"Node id '{node.id}' is used more than once.")
        else:
            nodes[node.id] = node

    edges: list[Edge] = []
    for edge in flow.edges:
        missing = [end for end in (edge.source, edge.target) if end not in nodes]
        if missing:
            node_id = edge.source if edge.source in nodes else None
            names = ", ".join(f"'{m}'" for m in missing)
            error(
                node_id, "dangling_edge", f"Edge {edge.source} -> {edge.target}: no node {names}."
            )
        else:
            edges.append(edge)
    outgoing: dict[str, list[Edge]] = defaultdict(list)
    for edge in edges:
        outgoing[edge.source].append(edge)

    starts = [node for node in nodes.values() if isinstance(node, StartNode)]
    if not starts:
        error(None, "no_start", "The flow has no start node.")
    for extra in starts[1:]:
        error(extra.id, "multiple_starts", "The flow must have exactly one start node.")

    for node in nodes.values():
        _check_fields(node, known_tools, error)
        _check_edges(node, outgoing[node.id], error)

    if starts:
        reachable = _reachable([start.id for start in starts], outgoing)
        if not any(isinstance(nodes[n], _TERMINAL) for n in reachable):
            error(starts[0].id, "no_exit_reachable", "No handoff or end node is reachable.")
        for node in nodes.values():
            if node.id not in reachable:
                error(node.id, "unreachable_node", "The node is not reachable from start.")

    return ValidationResult(errors=errors, warnings=_route_chain_warnings(nodes, outgoing))


def _check_fields(node: FlowNode, known_tools: Collection[str], error: _ErrorSink) -> None:
    def require(value: str, field: str) -> None:
        if not value.strip():
            error(node.id, "empty_field", f"'{field}' must not be empty.")

    def tool_known(name: str) -> None:
        if name.strip() and name not in known_tools:
            error(node.id, "unknown_tool", f"Tool '{name}' is not registered.")

    match node:
        case StartNode():
            require(node.data.greeting, "greeting")
        case LLMPromptNode():
            require(node.data.prompt, "prompt")
            for name in node.data.tools:
                require(name, "tools")
                tool_known(name)
            _check_route_names(node.id, node.data.routes, "routes", error)
        case ToolNode():
            require(node.data.tool, "tool")
            tool_known(node.data.tool)
        case KBAnswerNode():
            require(node.data.not_found_reply, "not_found_reply")
        case ConditionNode():
            _check_route_names(node.id, [c.route for c in node.data.cases], "cases.route", error)
        case HandoffNode():
            require(node.data.reason, "reason")
        case EndNode():
            require(node.data.farewell, "farewell")


def _check_route_names(node_id: str, routes: list[str], field: str, error: _ErrorSink) -> None:
    seen: set[str] = set()
    for route in routes:
        if not route.strip():
            error(node_id, "empty_field", f"'{field}' must not contain an empty route name.")
        elif route in seen:
            error(node_id, "duplicate_route", f"Route '{route}' is declared more than once.")
        seen.add(route)


def _routes(node: FlowNode) -> list[str]:
    if isinstance(node, LLMPromptNode):
        return node.data.routes
    if isinstance(node, ConditionNode):
        return [case.route for case in node.data.cases]
    return []


def _check_edges(node: FlowNode, edges: list[Edge], error: _ErrorSink) -> None:
    """Outgoing edges per node type, as in the node table of the SDD."""
    if isinstance(node, _TERMINAL):
        if edges:
            error(
                node.id,
                "edge_from_terminal",
                f"A {node.type} node ends the call; remove its edges.",
            )
        return

    routes = _routes(node)
    by_route: dict[str, int] = defaultdict(int)
    defaults = 0
    for edge in edges:
        if edge.when is None:
            defaults += 1
        elif edge.when.route in routes:
            by_route[edge.when.route] += 1
        else:
            error(
                node.id,
                "unknown_route_edge",
                f"Edge to '{edge.target}' uses undeclared route '{edge.when.route}'.",
            )

    for route in dict.fromkeys(r for r in routes if r.strip()):
        if by_route[route] == 0:
            error(node.id, "missing_route_edge", f"Route '{route}' has no edge.")
        elif by_route[route] > 1:
            error(node.id, "duplicate_route_edge", f"Route '{route}' has more than one edge.")

    # Pass-through nodes must lead somewhere; waiting nodes without a default edge stay current.
    if defaults == 0 and isinstance(node, _PASS_THROUGH):
        error(node.id, "missing_default_edge", "The node needs an edge without a condition.")
    elif defaults > 1:
        error(node.id, "multiple_default_edges", "Only one edge without a condition is allowed.")


def _reachable(roots: Iterable[str], outgoing: dict[str, list[Edge]]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id not in seen:
            seen.add(node_id)
            stack.extend(edge.target for edge in outgoing[node_id])
    return seen


def _same_turn(
    target: str, nodes: dict[str, FlowNode], outgoing: dict[str, list[Edge]]
) -> set[str]:
    """Nodes that can consume the current utterance after a route into ``target``.

    Pass-through nodes run in the same turn without the LLM, so they are walked through;
    the walk stops at waiting and terminal nodes.
    """
    found: set[str] = set()
    seen: set[str] = set()
    stack = [target]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(nodes[node_id], _PASS_THROUGH):
            stack.extend(edge.target for edge in outgoing[node_id])
        else:
            found.add(node_id)
    return found


def _route_chain_warnings(
    nodes: dict[str, FlowNode], outgoing: dict[str, list[Edge]]
) -> list[FlowIssue]:
    """Warn where a turn may need two empty LLM calls before the reply starts.

    After a route from ``llm_prompt`` A into ``llm_prompt`` B (same turn), B's call is the
    second one. It is fine if B answers or routes to a terminal node (canned text, no LLM).
    It breaks the budget if B may call a tool or route on to another LLM node.
    """
    warnings: dict[str, FlowIssue] = {}
    for source in nodes.values():
        if not isinstance(source, LLMPromptNode):
            continue
        for edge in outgoing[source.id]:
            if edge.when is None:
                continue  # the default edge is taken on the next turn
            for b_id in sorted(_same_turn(edge.target, nodes, outgoing)):
                b = nodes[b_id]
                if not isinstance(b, LLMPromptNode) or b_id in warnings:
                    continue
                via = f"Reached from '{source.id}' by route '{edge.when.route}' in the same turn"
                if b.data.tools:
                    reason = "it may call a tool, a second empty LLM call before the reply"
                elif _routes_to_llm(b, nodes, outgoing):
                    reason = "it may route on to another LLM node: two routes in a row"
                else:
                    continue
                warnings[b_id] = FlowIssue(
                    node_id=b_id, code="route_chain", message=f"{via}; {reason}."
                )
    return [warnings[n] for n in nodes if n in warnings]


def _routes_to_llm(
    node: LLMPromptNode, nodes: dict[str, FlowNode], outgoing: dict[str, list[Edge]]
) -> bool:
    return any(
        isinstance(nodes[n], LLMPromptNode | KBAnswerNode)
        for edge in outgoing[node.id]
        if edge.when is not None
        for n in _same_turn(edge.target, nodes, outgoing)
    )
