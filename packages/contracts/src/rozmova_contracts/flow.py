"""Flow graph shared by the visual editor and the flow executor.

A flow is a directed graph of typed nodes and edges, stored as JSON (ADR 0006). See the
SDD section "Флоу" for the node table and the execution semantics.

The models check shape only: types, enums and numeric ranges. A draft with an empty
prompt or a dangling edge must still save, so semantic checks (unique ids, edge targets,
reachability, one edge per route, required texts, known tools) belong to flow validation
(FR-9), which reports errors with ``node_id`` for the editor to highlight.
"""

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

_CONFIG = ConfigDict(extra="forbid", frozen=True)


class FlowModel(BaseModel):
    model_config = _CONFIG


class NodeUI(FlowModel):
    """Canvas position. Ignored by validation and by the version diff."""

    x: float
    y: float


# --- node data -------------------------------------------------------------------


class StartData(FlowModel):
    greeting: str


class LLMPromptData(FlowModel):
    prompt: str
    tools: list[str] = Field(default_factory=list, description="Tools the LLM may call.")
    routes: list[str] = Field(
        default_factory=list,
        description="Route names; the LLM gets a service action route(name) for each.",
    )


class ToolData(FlowModel):
    tool: str
    args: dict[str, JsonValue] = Field(
        default_factory=dict, description="Arguments; string values are templates over slots."
    )
    save_as: str | None = Field(default=None, description="Slot that receives the result.")


class KBAnswerData(FlowModel):
    top_k: int = Field(default=5, ge=1, le=20)
    min_similarity: float = Field(default=0.35, ge=0, le=1)
    not_found_reply: str


class ConditionCase(FlowModel):
    when: dict[str, JsonValue] = Field(description="JSONLogic rule over session slots.")
    route: str


class ConditionData(FlowModel):
    cases: list[ConditionCase] = Field(default_factory=list)


class HandoffData(FlowModel):
    reason: str


class EndData(FlowModel):
    farewell: str


# --- nodes -----------------------------------------------------------------------


class BaseNode(FlowModel):
    id: str
    ui: NodeUI | None = None


class StartNode(BaseNode):
    """Entry point; sends the greeting. One outgoing edge."""

    type: Literal["start"]
    data: StartData


class LLMPromptNode(BaseNode):
    """Waits for an utterance and answers with the LLM, or follows a route."""

    type: Literal["llm_prompt"]
    data: LLMPromptData


class ToolNode(BaseNode):
    """Deterministic tool call without the LLM. One outgoing edge."""

    type: Literal["tool"]
    data: ToolData


class KBAnswerNode(BaseNode):
    """Waits for an utterance and answers from knowledge-base chunks only. One edge."""

    type: Literal["kb_answer"]
    data: KBAnswerData


class ConditionNode(BaseNode):
    """Branches on slots without the LLM; the edge without ``when`` is the default."""

    type: Literal["condition"]
    data: ConditionData


class HandoffNode(BaseNode):
    """Transfers the call to an operator and ends it."""

    type: Literal["handoff"]
    data: HandoffData


class EndNode(BaseNode):
    """Says farewell and ends the call."""

    type: Literal["end"]
    data: EndData


FlowNode = Annotated[
    StartNode | LLMPromptNode | ToolNode | KBAnswerNode | ConditionNode | HandoffNode | EndNode,
    Field(discriminator="type"),
]

# --- edges and graph -------------------------------------------------------------


class EdgeWhen(FlowModel):
    route: str


class Edge(FlowModel):
    """A transition. Without ``when`` it is the node's unconditional edge."""

    source: str
    target: str
    when: EdgeWhen | None = None


class FlowGraph(FlowModel):
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def logic_dump(self) -> dict[str, Any]:
        """JSON-ready dump without ``nodes[].ui``: the part that versions and diffs compare."""
        return self.model_dump(mode="json", exclude={"nodes": {"__all__": {"ui"}}})

    def same_logic(self, other: "FlowGraph") -> bool:
        """Equal apart from canvas positions and the order of nodes and edges."""
        return _canonical(self.logic_dump()) == _canonical(other.logic_dump())

    def logic_hash(self) -> str:
        """SHA-256 of the logic: equal hashes iff ``same_logic``. Identifies a draft for its
        eval report, so moving a node on the canvas keeps the report valid."""
        canonical = json.dumps(_canonical(self.logic_dump()), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical(dump: dict[str, Any]) -> dict[str, list[str]]:
    return {
        key: sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items)
        for key, items in dump.items()
    }


flow_graph_adapter: TypeAdapter[FlowGraph] = TypeAdapter(FlowGraph)
