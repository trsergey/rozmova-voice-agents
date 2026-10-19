import demo from "../../../../../flows/hvylia-demo.json";
import { DEFAULT_OUTLET, type FlowGraph } from "./graph";
import { selectDirty, useEditor } from "./store";

const editor = () => useEditor.getState();
const edgesFrom = (source: string) => editor().graph.edges.filter((edge) => edge.source === source);

beforeEach(() => editor().load(demo as FlowGraph));

test("a loaded draft is clean; an edit makes it dirty and saving clears that", () => {
  expect(selectDirty(editor())).toBe(false);

  editor().addNode("end", 0, 0);
  expect(selectDirty(editor())).toBe(true);

  editor().markSaved(editor().graph);
  expect(selectDirty(editor())).toBe(false);
});

test("undo and redo walk the history; a new edit drops the redo branch", () => {
  const first = editor().addNode("end", 0, 0);
  const second = editor().addNode("handoff", 0, 0);
  expect([first, second]).toEqual(["end_1", "handoff_1"]);

  editor().undo();
  expect(editor().graph.nodes.map((n) => n.id)).not.toContain("handoff_1");
  editor().redo();
  expect(editor().graph.nodes.map((n) => n.id)).toContain("handoff_1");

  editor().undo();
  editor().undo();
  expect(selectDirty(editor())).toBe(false);
  editor().addNode("tool", 0, 0);
  expect(editor().future).toEqual([]);
});

test("a drag is one step, and a click without movement is none", () => {
  const before = editor().past.length;

  editor().beginDrag();
  editor().moveNodes([{ id: "start", x: 10, y: 10 }]);
  editor().moveNodes([{ id: "start", x: 50, y: 60 }]);
  editor().endDrag();
  expect(editor().past.length).toBe(before + 1);

  editor().beginDrag();
  editor().endDrag();
  expect(editor().past.length).toBe(before + 1);

  editor().undo();
  expect(editor().graph.nodes.find((n) => n.id === "start")?.ui).not.toEqual({ x: 50, y: 60 });
});

test("connecting from a route outlet labels the edge; a used outlet moves its edge", () => {
  editor().connect("assist", "tariff", "bye");
  expect(edgesFrom("assist").filter((e) => e.when?.route === "tariff")).toEqual([
    { source: "assist", target: "bye", when: { route: "tariff" } },
  ]);

  editor().connect("assist", DEFAULT_OUTLET, "promo");
  expect(edgesFrom("assist").filter((e) => !e.when)).toEqual([{ source: "assist", target: "promo" }]);

  const count = editor().graph.edges.length;
  editor().connect("assist", "assist", "assist");
  editor().connect("bye", DEFAULT_OUTLET, "bye");
  expect(editor().graph.edges.length).toBe(count);
});

test("removing a node removes its edges; removing an edge leaves the nodes", () => {
  editor().select("promo");
  editor().remove(["promo"], []);

  expect(editor().graph.nodes.map((n) => n.id)).not.toContain("promo");
  expect(editor().graph.edges.some((e) => e.source === "promo" || e.target === "promo")).toBe(false);
  expect(editor().selectedNodeId).toBeNull();

  const nodes = editor().graph.nodes.length;
  editor().remove([], [0]);
  expect(editor().graph.nodes.length).toBe(nodes);
  expect(editor().graph.edges[0]).not.toEqual({ source: "start", target: "assist" });

  editor().undo();
  editor().undo();
  expect(selectDirty(editor())).toBe(false);
});
