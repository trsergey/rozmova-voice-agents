// The editor page against a fake admin API: load, validate, edit, save, read-only, leave.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import demo from "../../../../../flows/hvylia-demo.json";
import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";

const BOT = {
  id: "b-1",
  name: "Hvylia demo",
  provider: "openai",
  model: "m",
  live_version_id: null,
  live_version: null,
  draft_updated_at: "2026-10-19T09:00:00Z",
  created_at: "2026-10-15T08:00:00Z",
  last_eval: null,
};
const ok = (body: unknown): Route => () => ({ status: 200, body });
const clean = { errors: [], warnings: [] };

// React Flow measures nodes and the viewport; jsdom has neither.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("DOMMatrixReadOnly", class {
    m22 = 1;
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
});

const api = (session = ADMIN, extra: Record<string, Route> = {}) => ({
  ...as(session),
  "GET /bots/b-1": ok(BOT),
  "GET /bots/b-1/draft": ok({ flow: demo, updated_at: "2026-10-19T09:00:00Z", hash: "h" }),
  "POST /bots/b-1/validate": ok(clean),
  "PUT /bots/b-1/draft": (body: unknown) => ({ status: 200, body: { flow: body, updated_at: "2026-10-19T10:00:00Z", hash: "h2" } }),
  ...extra,
});

test("the draft loads onto the canvas and is checked", async () => {
  const calls = fakeApi(api());
  open("/admin/bots/b-1/editor");

  expect(await screen.findByTestId("node-assist")).toHaveTextContent("LLM");
  expect(screen.getByTestId("node-tariff_kb")).toHaveTextContent("База знань");
  await screen.findByText("Помилок немає, флоу можна публікувати.");
  expect(calls.filter((call) => call.key === "POST /bots/b-1/validate")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Зберегти чернетку" })).toBeDisabled();
});

test("validator errors frame their nodes and are listed in words", async () => {
  fakeApi(
    api(ADMIN, {
      "POST /bots/b-1/validate": ok({
        errors: [{ node_id: "promo", code: "missing_route_edge", message: "Route 'done' has no edge." }],
        warnings: [],
      }),
    }),
  );
  open("/admin/bots/b-1/editor");

  const promo = await screen.findByTestId("node-promo");
  expect(promo).toHaveTextContent("1 помилка");
  expect(promo).toHaveAttribute("title", "Маршрут не має ребра.");
  const panel = screen.getByRole("complementary", { name: "Перевірка" });
  expect(await within(panel).findByText("Помилок: 1")).toBeInTheDocument();
  expect(panel).toHaveTextContent("promoМаршрут не має ребра.");
});

test("an added node makes the draft dirty, undo reverts it, save sends the graph and re-checks", async () => {
  const calls = fakeApi(api());
  open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");

  const user = userEvent.setup();
  const palette = screen.getByRole("complementary", { name: "Палітра" });
  await user.click(within(palette).getByRole("button", { name: /Кінець/ }));
  expect(await screen.findByTestId("node-end_1")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Є незбережені зміни");

  await user.click(screen.getByRole("button", { name: "Скасувати" }));
  expect(screen.queryByTestId("node-end_1")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).not.toHaveTextContent("незбережені");

  await user.click(screen.getByRole("button", { name: "Повторити" }));
  await user.click(screen.getByRole("button", { name: "Зберегти чернетку" }));

  await vi.waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/^Збережено о/));
  const put = calls.find((call) => call.key === "PUT /bots/b-1/draft");
  const body = put?.body as { nodes: { id: string; ui: unknown }[]; edges: unknown[] };
  expect(put?.csrf).toBe("csrf-admin");
  expect(body.nodes.map((n) => n.id)).toEqual([...demo.nodes.map((n) => n.id), "end_1"]);
  expect(body.nodes.every((n) => n.ui)).toBe(true);
  expect(body.edges).toEqual(demo.edges);
  await vi.waitFor(() => expect(calls.filter((call) => call.key === "POST /bots/b-1/validate")).toHaveLength(2));
});

test("Ctrl+S saves and Ctrl+Z undoes", async () => {
  const calls = fakeApi(api());
  open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");

  const user = userEvent.setup();
  await user.click(within(screen.getByRole("complementary", { name: "Палітра" })).getByRole("button", { name: /Оператор/ }));
  await user.keyboard("{Control>}z{/Control}");
  expect(screen.queryByTestId("node-handoff_1")).not.toBeInTheDocument();

  await user.keyboard("{Control>}{Shift>}z{/Shift}{/Control}");
  await user.keyboard("{Control>}s{/Control}");
  await vi.waitFor(() => expect(calls.some((call) => call.key === "PUT /bots/b-1/draft")).toBe(true));
});

test("leaving with unsaved changes asks first", async () => {
  fakeApi(api());
  const router = open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");

  const user = userEvent.setup();
  await user.click(within(screen.getByRole("complementary", { name: "Палітра" })).getByRole("button", { name: /Дія/ }));
  await user.click(screen.getByRole("link", { name: "← Hvylia demo" }));

  const dialog = await screen.findByRole("alertdialog");
  await user.click(within(dialog).getByRole("button", { name: "Залишитись" }));
  expect(router.state.location.pathname).toBe("/admin/bots/b-1/editor");

  await user.click(screen.getByRole("link", { name: "← Hvylia demo" }));
  await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Піти" }));
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/admin/bots/b-1"));
});

test("viewers see the canvas read-only", async () => {
  fakeApi(api(VIEWER, { "GET /bots/b-1/credentials": ok([]) }));
  open("/admin/bots/b-1/editor");

  await screen.findByTestId("node-assist");
  expect(screen.queryByRole("complementary", { name: "Палітра" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Зберегти чернетку" })).not.toBeInTheDocument();
});
