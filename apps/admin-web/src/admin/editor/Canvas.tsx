// The React Flow canvas over the editor store. The store owns the graph and the selected
// node (the side panel edits it); React Flow's measured node sizes and edge selection stay
// here and are merged into what it renders.
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState, type DragEvent } from "react";

import { FlowNodeView, type CanvasNode } from "./FlowNodeView";
import { DEFAULT_OUTLET, outletOf, type NodeType } from "./graph";
import { useEditor } from "./store";

const nodeTypes = { flow: FlowNodeView };
export const PALETTE_MIME = "application/x-rozmova-node";

interface Meta {
  measured?: { width?: number; height?: number };
}

export function Canvas({ editable, activeNodeId = null }: { editable: boolean; activeNodeId?: string | null }) {
  const graph = useEditor((state) => state.graph);
  const issues = useEditor((state) => state.issues);
  const selectedNodeId = useEditor((state) => state.selectedNodeId);
  const { beginDrag, endDrag, moveNodes, connect, remove, select, addNode } = useEditor.getState();
  const [meta, setMeta] = useState<Record<string, Meta>>({});
  const [selectedEdges, setSelectedEdges] = useState<Set<string>>(new Set());
  const [flow, setFlow] = useState<ReactFlowInstance<CanvasNode, Edge> | null>(null);

  const nodes = useMemo<CanvasNode[]>(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: "flow",
        position: node.ui ?? { x: 0, y: 0 },
        data: { node, issues: issues.filter((issue) => issue.node_id === node.id), active: node.id === activeNodeId },
        selected: node.id === selectedNodeId,
        ...meta[node.id],
      })),
    [graph.nodes, issues, meta, activeNodeId, selectedNodeId],
  );

  const edges = useMemo<Edge[]>(
    () =>
      graph.edges.map((edge, index) => {
        const id = `e${index}`;
        return {
          id,
          source: edge.source,
          target: edge.target,
          sourceHandle: outletOf(edge),
          label: edge.when?.route,
          selected: selectedEdges.has(id),
          markerEnd: { type: MarkerType.ArrowClosed },
          labelBgPadding: [4, 2] as [number, number],
          className: "text-xs",
        };
      }),
    [graph.edges, selectedEdges],
  );

  const onNodesChange = (changes: NodeChange<CanvasNode>[]) => {
    const moves: { id: string; x: number; y: number }[] = [];
    setMeta((current) => {
      let next = current;
      for (const change of changes) {
        if (change.type === "dimensions" && change.dimensions) {
          next = { ...next, [change.id]: { ...next[change.id], measured: change.dimensions } };
        }
      }
      return next;
    });
    for (const change of changes) {
      if (change.type === "position" && change.position && editable) {
        moves.push({ id: change.id, ...change.position });
      } else if (change.type === "select") {
        // A click: select this node, or clear the selection it held.
        if (change.selected) select(change.id);
        else if (useEditor.getState().selectedNodeId === change.id) select(null);
      }
    }
    if (moves.length > 0) moveNodes(moves);
  };

  const onEdgesChange = (changes: EdgeChange[]) => {
    setSelectedEdges((current) => {
      const next = new Set(current);
      for (const change of changes) {
        if (change.type !== "select") continue;
        if (change.selected) next.add(change.id);
        else next.delete(change.id);
      }
      return next;
    });
  };

  const onConnect = (connection: Connection) =>
    connect(connection.source, connection.sourceHandle ?? DEFAULT_OUTLET, connection.target);

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    const type = event.dataTransfer.getData(PALETTE_MIME) as NodeType;
    if (!type || !flow) return;
    const position = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
    select(addNode(type, position.x, position.y));
  };

  return (
    <ReactFlow<CanvasNode, Edge>
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onInit={setFlow}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodeDragStart={beginDrag}
      onNodeDragStop={endDrag}
      onDelete={({ nodes: gone, edges: dropped }) =>
        remove(
          gone.map((node) => node.id),
          dropped.map((edge) => Number(edge.id.slice(1))),
        )
      }
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
      }}
      onDrop={onDrop}
      nodesDraggable={editable}
      nodesConnectable={editable}
      elementsSelectable
      selectionKeyCode={null}
      multiSelectionKeyCode={null}
      deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
      colorMode="system"
      fitView
      // Never fit by shrinking the nodes below readable: a wide flow opens at its left part
      // and the minimap shows the rest.
      fitViewOptions={{ padding: 0.15, minZoom: 0.8 }}
      minZoom={0.2}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable className="!hidden md:!block" />
    </ReactFlow>
  );
}
