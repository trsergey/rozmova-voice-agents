// The node settings forms inside the editor page, against a fake admin API.
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import demo from "../../../../../flows/hvylia-demo.json";
import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";
import { useEditor } from "./store";

const BOT = {
  id: "b-1",
  name: "Hvylia demo",
  provider: "openai",
  model: "m",
  live_version_id: null,
  live_version: null,
  draft_updated_at: null,
  created_at: "2026-10-15T08:00:00Z",
  last_eval: null,
};
const TOOLS = [
  { name: "activate_service", description: "Підключає послугу.", parameters: { type: "object" }, side_effect: true },
  { name: "get_balance", description: "Баланс абонента.", parameters: { type: "object" }, side_effect: false },
];
const ok = (body: unknown): Route => () => ({ status: 200, body });

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("DOMMatrixReadOnly", class {
    m22 = 1;
  });
});
afterEach(() => vi.unstubAllGlobals());

const api = (session = ADMIN, validate: unknown = { errors: [], warnings: [] }) => ({
  ...as(session),
  "GET /bots/b-1": ok(BOT),
  "GET /bots/b-1/draft": ok({ flow: demo, updated_at: null, hash: "h" }),
  "POST /bots/b-1/validate": ok(validate),
  "GET /tools": ok(TOOLS),
});

async function openNode(id: string, session = ADMIN, validate?: unknown) {
  fakeApi(api(session, validate));
  open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");
  act(() => useEditor.getState().select(id));
  return loadedPanel();
}

// The panel, once its lazily loaded form is there.
async function loadedPanel() {
  const panel = await screen.findByRole("complementary", { name: "Налаштування вузла" });
  await within(panel).findAllByRole("group");
  return panel;
}

const node = (id: string) => useEditor.getState().graph.nodes.find((n) => n.id === id);

test("typing a greeting updates the node at once and marks the draft unsaved", async () => {
  const panel = await openNode("start");

  const user = userEvent.setup();
  const greeting = within(panel).getByLabelText("Вітання");
  await user.clear(greeting);
  await user.type(greeting, "Добрий день!");

  expect(node("start")?.data).toEqual({ greeting: "Добрий день!" });
  expect(screen.getByTestId("node-start")).toHaveTextContent("Добрий день!");
  expect(screen.getByRole("status")).toHaveTextContent("Є незбережені зміни");
});

test("an LLM node picks tools from the registry and renames a route with its edge", async () => {
  const panel = await openNode("assist");

  const user = userEvent.setup();
  // The demo node names four tools; this registry knows two, the others stay listed.
  expect(await within(panel).findByRole("checkbox", { name: /get_balance/ })).toBeChecked();
  expect(within(panel).getByRole("checkbox", { name: /list_services.*немає в реєстрі/ })).toBeChecked();
  expect(within(panel).getByText("змінює дані")).toBeInTheDocument();
  await user.click(within(panel).getByRole("checkbox", { name: /get_balance/ }));
  const tools = (node("assist")?.data as { tools: string[] }).tools;
  expect([...tools].sort()).toEqual(["activate_service", "get_current_tariff", "list_services"]);

  const routes = within(panel).getAllByRole("textbox", { name: /^Маршрут \d$/ });
  const tariff = routes.find((input) => (input as HTMLInputElement).value === "tariff");
  if (!tariff) throw new Error("the tariff route is listed");
  await user.clear(tariff);
  await user.type(tariff, "tariffs");

  const edge = useEditor.getState().graph.edges.find((e) => e.source === "assist" && e.target === "tariff_kb");
  expect(edge?.when).toEqual({ route: "tariffs" });

  await user.click(within(panel).getByRole("button", { name: "Додати маршрут" }));
  expect((node("assist")?.data as { routes: string[] }).routes).toContain("route_4");
});

test("tool arguments must be a JSON object; until then the node keeps its last valid data", async () => {
  fakeApi(api());
  open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");
  act(() => {
    const id = useEditor.getState().addNode("tool", 0, 0);
    useEditor.getState().select(id);
  });
  const panel = await loadedPanel();

  const user = userEvent.setup();
  await within(panel).findByRole("option", { name: "activate_service" });
  await user.selectOptions(within(panel).getByLabelText("Дія"), "activate_service");
  expect(within(panel).getByText("Підключає послугу.")).toBeInTheDocument();

  const args = within(panel).getByLabelText(/^Аргументи/);
  await user.clear(args);
  await user.type(args, '{{"service_id": "roaming"');
  expect(await within(panel).findByRole("alert")).toHaveTextContent("Потрібен JSON-об'єкт");
  expect(node("tool_1")?.data).toMatchObject({ tool: "activate_service", args: {} });

  await user.type(args, "}");
  await vi.waitFor(() => expect(node("tool_1")?.data).toMatchObject({ args: { service_id: "roaming" } }));
  await user.type(within(panel).getByLabelText("Зберегти результат у слот"), "activation");
  expect(node("tool_1")?.data).toMatchObject({ save_as: "activation" });
});

test("KB numbers out of the contract's range are refused", async () => {
  const panel = await openNode("tariff_kb");

  const user = userEvent.setup();
  const topK = within(panel).getByLabelText("Фрагментів (top k)");
  await user.clear(topK);
  await user.type(topK, "50");

  expect(await within(panel).findByRole("alert")).toHaveTextContent("від 1 до 20");
  expect(node("tariff_kb")?.data).toMatchObject({ top_k: 5 });

  await user.clear(topK);
  await user.type(topK, "8");
  await vi.waitFor(() => expect(node("tariff_kb")?.data).toMatchObject({ top_k: 8 }));
});

test("the validator's issues show under their field", async () => {
  const panel = await openNode("bye", ADMIN, {
    errors: [{ node_id: "bye", code: "empty_field", message: "'farewell' must not be empty." }],
    warnings: [],
  });

  const field = within(panel).getByLabelText("Прощання").parentElement;
  expect(await within(field as HTMLElement).findByText("Не заповнене обов'язкове поле.")).toBeInTheDocument();
});

test("deleting from the panel removes the node and its edges", async () => {
  const panel = await openNode("promo");

  await userEvent.setup().click(within(panel).getByRole("button", { name: "Видалити вузол" }));

  expect(node("promo")).toBeUndefined();
  expect(screen.queryByRole("complementary", { name: "Налаштування вузла" })).not.toBeInTheDocument();
});

test("viewers see the settings but cannot change them", async () => {
  const panel = await openNode("start", VIEWER);

  expect(within(panel).getByLabelText("Вітання")).toBeDisabled();
  expect(within(panel).queryByRole("button", { name: "Видалити вузол" })).not.toBeInTheDocument();
});
