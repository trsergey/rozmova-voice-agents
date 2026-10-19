// The flow graph as the editor sees it: node kinds, their outlets (one per route, plus the
// edge without a condition where the node type has one), new nodes, and a layered layout
// for flows saved without canvas positions. Pure functions over the FlowGraph contract.
import type { Schemas } from "../../api/client";

export type FlowGraph = Schemas["FlowGraph"];
export type FlowNode = NonNullable<FlowGraph["nodes"]>[number];
export type FlowEdge = Schemas["Edge"];
export type NodeType = FlowNode["type"];
export type FlowIssue = Schemas["FlowIssue"];

// A complete graph: both lists present, every node positioned.
export interface Graph {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

// The outlet of an edge without `when`.
export const DEFAULT_OUTLET = "default";

export interface Outlet {
  id: string; // a route name, or DEFAULT_OUTLET
  label: string | null;
}

export const NODE_KINDS: Record<NodeType, { label: string; hint: string; accent: string }> = {
  start: { label: "Старт", hint: "Вітання й точка входу", accent: "border-t-emerald-500" },
  llm_prompt: { label: "LLM", hint: "Відповідь моделі, дії, маршрути", accent: "border-t-sky-500" },
  kb_answer: { label: "База знань", hint: "Відповідь лише з фрагментів БЗ", accent: "border-t-violet-500" },
  tool: { label: "Дія", hint: "Виклик дії без LLM", accent: "border-t-amber-500" },
  condition: { label: "Умова", hint: "Розгалуження за слотами", accent: "border-t-orange-500" },
  handoff: { label: "Оператор", hint: "Передача оператору", accent: "border-t-rose-500" },
  end: { label: "Кінець", hint: "Прощання й завершення", accent: "border-t-slate-500" },
};

export const PALETTE: NodeType[] = ["start", "llm_prompt", "kb_answer", "tool", "condition", "handoff", "end"];

// Where edges may leave a node (SDD node table): handoff and end end the call.
export function outlets(node: FlowNode): Outlet[] {
  const routes = (names: string[]) => names.map((name) => ({ id: name, label: name }));
  switch (node.type) {
    case "start":
    case "tool":
    case "kb_answer":
      return [{ id: DEFAULT_OUTLET, label: null }];
    case "llm_prompt":
      return [...routes(node.data.routes ?? []), { id: DEFAULT_OUTLET, label: "інакше" }];
    case "condition":
      return [...routes((node.data.cases ?? []).map((c) => c.route)), { id: DEFAULT_OUTLET, label: "інакше" }];
    case "handoff":
    case "end":
      return [];
  }
}

export function hasInlet(node: FlowNode): boolean {
  return node.type !== "start";
}

export function outletOf(edge: FlowEdge): string {
  return edge.when?.route ?? DEFAULT_OUTLET;
}

export function newNode(type: NodeType, id: string, x: number, y: number): FlowNode {
  const ui = { x, y };
  switch (type) {
    case "start":
      return { id, ui, type, data: { greeting: "" } };
    case "llm_prompt":
      return { id, ui, type, data: { prompt: "", tools: [], routes: [] } };
    case "tool":
      return { id, ui, type, data: { tool: "", args: {}, save_as: null } };
    case "kb_answer":
      return { id, ui, type, data: { top_k: 5, min_similarity: 0.35, not_found_reply: "" } };
    case "condition":
      return { id, ui, type, data: { cases: [] } };
    case "handoff":
      return { id, ui, type, data: { reason: "" } };
    case "end":
      return { id, ui, type, data: { farewell: "" } };
  }
}

export function uniqueId(type: NodeType, taken: Iterable<string>): string {
  const used = new Set(taken);
  for (let n = 1; ; n++) {
    const id = `${type}_${n}`;
    if (!used.has(id)) return id;
  }
}

export const LAYOUT = { columnGap: 280, rowGap: 150 };

// Positions for nodes without `ui`: columns by distance from start along the edges (a
// node first reached at depth d sits in column d), rows in the order they were reached.
// Nodes not reachable from start go to a last column. Positioned nodes keep their place.
export function layout(nodes: FlowNode[], edges: FlowEdge[]): FlowNode[] {
  if (nodes.every((node) => node.ui)) return nodes;
  const depth = new Map<string, number>();
  const start = nodes.find((node) => node.type === "start") ?? nodes[0];
  const queue: string[] = [];
  if (start) {
    depth.set(start.id, 0);
    queue.push(start.id);
  }
  while (queue.length > 0) {
    const id = queue.shift() as string;
    for (const edge of edges) {
      if (edge.source === id && !depth.has(edge.target)) {
        depth.set(edge.target, (depth.get(id) ?? 0) + 1);
        queue.push(edge.target);
      }
    }
  }
  const lastColumn = Math.max(-1, ...depth.values()) + 1;
  const rows = new Map<number, number>();
  return nodes.map((node) => {
    if (node.ui) return node;
    const column = depth.get(node.id) ?? lastColumn;
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    return { ...node, ui: { x: column * LAYOUT.columnGap, y: row * LAYOUT.rowGap } };
  });
}

export function toGraph(flow: FlowGraph): Graph {
  const edges = flow.edges ?? [];
  return { nodes: layout(flow.nodes ?? [], edges), edges };
}

// What PUT /bots/{id}/draft gets: positions rounded so a save does not churn on sub-pixels.
export function toFlowGraph(graph: Graph): FlowGraph {
  return {
    nodes: graph.nodes.map((node) =>
      node.ui ? { ...node, ui: { x: Math.round(node.ui.x), y: Math.round(node.ui.y) } } : node,
    ),
    edges: graph.edges,
  };
}

// One line under the node title, so the canvas reads without opening a form.
export function summary(node: FlowNode): string {
  const cut = (text: string) => (text.length > 60 ? `${text.slice(0, 57)}…` : text);
  switch (node.type) {
    case "start":
      return cut(node.data.greeting) || "без вітання";
    case "llm_prompt": {
      const tools = node.data.tools?.length ?? 0;
      return cut(node.data.prompt) || (tools ? `дій: ${tools}` : "без промпту");
    }
    case "tool":
      return node.data.tool || "дію не вибрано";
    case "kb_answer":
      return `top ${node.data.top_k ?? 5} · поріг ${node.data.min_similarity ?? 0.35}`;
    case "condition":
      return `умов: ${node.data.cases?.length ?? 0}`;
    case "handoff":
      return cut(node.data.reason) || "без причини";
    case "end":
      return cut(node.data.farewell) || "без прощання";
  }
}

// The validator's codes in words (its messages are for developers and in English).
export const ISSUE_TEXT: Record<FlowIssue["code"], string> = {
  no_start: "У флоу немає вузла «Старт».",
  multiple_starts: "Вузол «Старт» має бути один.",
  duplicate_node_id: "Id вузла повторюється.",
  dangling_edge: "Ребро веде до вузла, якого немає.",
  no_exit_reachable: "Від старту не досяжний ні «Оператор», ні «Кінець».",
  unreachable_node: "Вузол не досяжний від старту.",
  missing_route_edge: "Маршрут не має ребра.",
  duplicate_route_edge: "Маршрут має більше одного ребра.",
  unknown_route_edge: "Ребро використовує неоголошений маршрут.",
  missing_default_edge: "Потрібне ребро без умови.",
  multiple_default_edges: "Ребро без умови має бути одне.",
  edge_from_terminal: "Цей вузол завершує дзвінок; ребер з нього бути не може.",
  empty_field: "Не заповнене обов'язкове поле.",
  duplicate_route: "Назва маршруту повторюється.",
  unknown_tool: "Такої дії немає в реєстрі.",
  route_chain: "Маршрути утворюють ланцюжок без відповіді абоненту.",
};
