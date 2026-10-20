// The editor's state (SDD "Адмінка"): the graph being edited, undo/redo history, the
// last saved version (for the unsaved-changes mark) and the validator's issues. Every edit
// is one history step; a node drag is one step however many moves it makes.
import { create } from "zustand";

import {
  DEFAULT_OUTLET,
  newNode,
  outletOf,
  routesOf,
  sameJson,
  syncRouteEdges,
  toGraph,
  uniqueId,
  type FlowGraph,
  type FlowIssue,
  type FlowNode,
  type Graph,
  type NodeType,
} from "./graph";

const HISTORY_LIMIT = 100;
// Edits of one node's settings this close together are one undo step (typing a prompt).
const COALESCE_MS = 1000;

interface EditorState {
  graph: Graph;
  past: Graph[];
  future: Graph[];
  saved: string; // the serialized graph as last loaded or saved
  issues: FlowIssue[];
  selectedNodeId: string | null;

  load: (flow: FlowGraph) => void;
  // Swap the whole canvas for another flow (e.g. the live version) as one undoable edit.
  replace: (flow: FlowGraph) => void;
  addNode: (type: NodeType, x: number, y: number) => string;
  beginDrag: () => void;
  endDrag: () => void;
  moveNodes: (moves: { id: string; x: number; y: number }[]) => void;
  connect: (source: string, outlet: string, target: string) => void;
  updateData: (nodeId: string, data: FlowNode["data"]) => void;
  remove: (nodeIds: string[], edgeIndexes: number[]) => void;
  undo: () => void;
  redo: () => void;
  markSaved: (graph: Graph) => void;
  setIssues: (issues: FlowIssue[]) => void;
  select: (nodeId: string | null) => void;
}

const serialize = (graph: Graph) => JSON.stringify(graph);
const empty: Graph = { nodes: [], edges: [] };

export const useEditor = create<EditorState>()((set, get) => {
  let lastEdit: { key: string; at: number } | null = null;

  // Apply an edit as one undoable step, or fold it into the previous step when it has the
  // same coalescing key and follows it closely.
  const edit = (next: Graph, coalesceKey?: string) => {
    const now = Date.now();
    const fold = coalesceKey !== undefined && lastEdit?.key === coalesceKey && now - lastEdit.at < COALESCE_MS;
    lastEdit = coalesceKey === undefined ? null : { key: coalesceKey, at: now };
    set((state) =>
      fold
        ? { graph: next, future: [] }
        : { graph: next, past: [...state.past, state.graph].slice(-HISTORY_LIMIT), future: [] },
    );
  };

  return {
    graph: empty,
    past: [],
    future: [],
    saved: serialize(empty),
    issues: [],
    selectedNodeId: null,

    load: (flow) => {
      lastEdit = null;
      const graph = toGraph(flow);
      set({ graph, past: [], future: [], saved: serialize(graph), issues: [], selectedNodeId: null });
    },

    replace: (flow) => {
      lastEdit = null;
      edit(toGraph(flow));
      set({ selectedNodeId: null });
    },

    addNode: (type, x, y) => {
      const { graph } = get();
      const id = uniqueId(
        type,
        graph.nodes.map((node) => node.id),
      );
      edit({ ...graph, nodes: [...graph.nodes, newNode(type, id, x, y)] });
      return id;
    },

    // A drag moves nodes many times; the step to undo is the whole drag. A click that
    // moved nothing leaves no step behind.
    beginDrag: () => edit(get().graph),
    endDrag: () =>
      set((state) => {
        const before = state.past.at(-1);
        return before && serialize(before) === serialize(state.graph) ? { past: state.past.slice(0, -1) } : state;
      }),

    moveNodes: (moves) =>
      set((state) => {
        const at = new Map(moves.map((move) => [move.id, move]));
        return {
          graph: {
            ...state.graph,
            nodes: state.graph.nodes.map((node) => {
              const move = at.get(node.id);
              return move ? { ...node, ui: { x: move.x, y: move.y } } : node;
            }),
          },
        };
      }),

    // An outlet carries one edge (one per route, one without a condition), so connecting
    // from a used outlet moves its edge to the new target.
    connect: (source, outlet, target) => {
      const { graph } = get();
      if (source === target) return;
      const edge = outlet === DEFAULT_OUTLET ? { source, target } : { source, target, when: { route: outlet } };
      const kept = graph.edges.filter((e) => !(e.source === source && outletOf(e) === outlet));
      edit({ ...graph, edges: [...kept, edge] });
    },

    updateData: (nodeId, data) => {
      const { graph } = get();
      const node = graph.nodes.find((n) => n.id === nodeId);
      if (!node || sameJson(node.data, data)) return;
      const updated = { ...node, data } as FlowNode;
      edit(
        {
          nodes: graph.nodes.map((n) => (n.id === nodeId ? updated : n)),
          edges: syncRouteEdges(graph.edges, nodeId, routesOf(node), routesOf(updated)),
        },
        `data:${nodeId}`,
      );
    },

    remove: (nodeIds, edgeIndexes) => {
      if (nodeIds.length === 0 && edgeIndexes.length === 0) return;
      const { graph, selectedNodeId } = get();
      const gone = new Set(nodeIds);
      const dropped = new Set(edgeIndexes);
      edit({
        nodes: graph.nodes.filter((node) => !gone.has(node.id)),
        edges: graph.edges.filter(
          (edge, index) => !dropped.has(index) && !gone.has(edge.source) && !gone.has(edge.target),
        ),
      });
      if (selectedNodeId && gone.has(selectedNodeId)) set({ selectedNodeId: null });
    },

    undo: () => {
      lastEdit = null;
      set((state) => {
        const previous = state.past.at(-1);
        if (!previous) return state;
        return { graph: previous, past: state.past.slice(0, -1), future: [state.graph, ...state.future] };
      });
    },

    redo: () => {
      lastEdit = null;
      set((state) => {
        const [next, ...rest] = state.future;
        if (!next) return state;
        return { graph: next, past: [...state.past, state.graph], future: rest };
      });
    },

    markSaved: (graph) => set({ saved: serialize(graph) }),
    setIssues: (issues) => set({ issues }),
    select: (nodeId) => set({ selectedNodeId: nodeId }),
  };
});

export const selectDirty = (state: EditorState) => serialize(state.graph) !== state.saved;
