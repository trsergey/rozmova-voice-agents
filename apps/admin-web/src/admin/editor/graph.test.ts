import demo from "../../../../../flows/hvylia-demo.json";
import {
  DEFAULT_OUTLET,
  layout,
  LAYOUT,
  newNode,
  outlets,
  PALETTE,
  syncRouteEdges,
  toFlowGraph,
  toGraph,
  uniqueId,
  type FlowGraph,
  type FlowNode,
} from "./graph";

const DEMO = demo as FlowGraph;

test("outlets follow the node table: a route each, a default where allowed, none on terminals", () => {
  const llm = newNode("llm_prompt", "assist", 0, 0);
  if (llm.type !== "llm_prompt") throw new Error("llm_prompt expected");
  llm.data.routes = ["tariff", "bye"];

  expect(outlets(llm).map((o) => o.id)).toEqual(["tariff", "bye", DEFAULT_OUTLET]);
  expect(outlets(newNode("start", "s", 0, 0))).toEqual([{ id: DEFAULT_OUTLET, label: null }]);
  expect(outlets(newNode("kb_answer", "k", 0, 0)).map((o) => o.id)).toEqual([DEFAULT_OUTLET]);
  expect(outlets(newNode("handoff", "h", 0, 0))).toEqual([]);
  expect(outlets(newNode("end", "e", 0, 0))).toEqual([]);
});

test("every palette type makes a node the contract accepts", () => {
  const ids = PALETTE.map((type) => newNode(type, `${type}_1`, 10, 20));

  expect(ids.map((node) => node.type)).toEqual(PALETTE);
  expect(ids.every((node) => node.ui?.x === 10 && node.ui.y === 20)).toBe(true);
});

test("ids are unique per type", () => {
  expect(uniqueId("tool", ["tool_1", "tool_2", "end_3"])).toBe("tool_3");
  expect(uniqueId("end", [])).toBe("end_1");
});

test("the demo flow without positions is laid out in columns by distance from start", () => {
  const graph = toGraph(DEMO);
  const at = Object.fromEntries(graph.nodes.map((node) => [node.id, node.ui]));

  // start → assist → tariff_kb | promo | bye → operator
  expect(at.start).toEqual({ x: 0, y: 0 });
  expect(at.assist).toEqual({ x: LAYOUT.columnGap, y: 0 });
  expect([at.tariff_kb?.x, at.promo?.x, at.bye?.x]).toEqual(Array(3).fill(2 * LAYOUT.columnGap));
  expect(new Set([at.tariff_kb?.y, at.promo?.y, at.bye?.y]).size).toBe(3);
  expect(at.operator?.x).toBe(3 * LAYOUT.columnGap);
});

test("positioned nodes keep their place; unreachable ones go to a last column", () => {
  const placed: FlowNode = { ...newNode("start", "start", 500, 500) };
  const orphan: FlowNode = { ...newNode("end", "orphan", 0, 0), ui: null };
  const next: FlowNode = { ...newNode("end", "bye", 0, 0), ui: null };

  const [start, lost, bye] = layout([placed, orphan, next], [{ source: "start", target: "bye" }]);

  expect(start?.ui).toEqual({ x: 500, y: 500 });
  expect(bye?.ui).toEqual({ x: LAYOUT.columnGap, y: 0 });
  expect(lost?.ui).toEqual({ x: 2 * LAYOUT.columnGap, y: 0 });
});

test("saving keeps the logic exactly and rounds positions", () => {
  const [first, ...others] = toGraph(DEMO).nodes;
  if (!first) throw new Error("the demo flow has nodes");
  const graph = { nodes: [{ ...first, ui: { x: 10.4, y: 20.6 } }, ...others], edges: DEMO.edges ?? [] };
  const logic = (nodes: FlowNode[] = []) => nodes.map((node) => ({ ...node, ui: undefined }));

  const flow = toFlowGraph(graph);

  expect(flow.edges).toEqual(DEMO.edges);
  expect(logic(flow.nodes)).toEqual(logic(DEMO.nodes));
  expect(flow.nodes?.[0]?.ui).toEqual({ x: 10, y: 21 });
});

test("route edges follow renamed routes and leave with removed ones", () => {
  const edges = [
    { source: "a", target: "x", when: { route: "tariff" } },
    { source: "a", target: "y", when: { route: "bye" } },
    { source: "a", target: "z" },
    { source: "b", target: "x", when: { route: "tariff" } },
  ];

  expect(syncRouteEdges(edges, "a", ["tariff", "bye"], ["tariffs", "bye"])).toEqual([
    { source: "a", target: "x", when: { route: "tariffs" } },
    edges[1],
    edges[2],
    edges[3],
  ]);
  expect(syncRouteEdges(edges, "a", ["tariff", "bye"], ["bye"])).toEqual([edges[1], edges[2], edges[3]]);
  expect(syncRouteEdges(edges, "a", ["tariff", "bye"], ["tariff", "bye", "new"])).toEqual(edges);
});
