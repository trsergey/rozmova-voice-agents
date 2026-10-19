// The call log pages against a fake admin API and a fake live feed.
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";

const BOT = { id: "b-1", name: "Hvylia demo", provider: "openai", model: "m", live_version_id: "v", live_version: 3, draft_updated_at: null, created_at: "2026-10-15T08:00:00Z" };
const CALL_ID = "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c";
const CALL = {
  id: CALL_ID,
  bot_id: "b-1",
  bot_name: "Hvylia demo",
  bot_version: 3,
  started_at: "2026-10-19T10:00:00Z",
  ended_at: "2026-10-19T10:01:05Z",
  outcome: "handoff",
  caller: "Acme",
  turns: 2,
};
const EVENTS = [
  { id: "1-0", ts: "2026-10-19T10:00:00Z", type: "session_started", payload: { bot_version: 3, resumed: false } },
  { id: "2-0", ts: "2026-10-19T10:00:02Z", type: "user_message", payload: { text: "мій баланс?" } },
  { id: "3-0", ts: "2026-10-19T10:00:02Z", type: "node_enter", payload: { node_id: "assist" } },
  { id: "4-0", ts: "2026-10-19T10:00:03Z", type: "tool_call", payload: { turn_id: 1, name: "get_balance", args: { msisdn: "380" } } },
  { id: "5-0", ts: "2026-10-19T10:00:03Z", type: "tool_result", payload: { turn_id: 1, name: "get_balance", result: { balance: 120 } } },
  { id: "6-0", ts: "2026-10-19T10:00:04Z", type: "assistant_message", payload: { turn_id: 1, text: "На рахунку 120 грн.", interrupted: false } },
  {
    id: "7-0",
    ts: "2026-10-19T10:00:04Z",
    type: "turn_end",
    payload: { turn_id: 1, timings: { total_ms: 2000, ttft_ms: 850, stages: { llm_ttft: 800, tools: 300 } }, sources: [] },
  },
  { id: "8-0", ts: "2026-10-19T10:00:30Z", type: "error", payload: { code: "tool_timeout", message: "Billing is slow" } },
  { id: "9-0", ts: "2026-10-19T10:01:05Z", type: "session_end", payload: { reason: "handoff" } },
];

const ok = (body: unknown): Route => () => ({ status: 200, body });

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {}

  open() {
    act(() => this.onopen?.());
  }

  push(event: Record<string, unknown>) {
    act(() => this.onmessage?.({ data: JSON.stringify(event) }));
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => vi.unstubAllGlobals());

const api = (session = ADMIN, extra: Record<string, Route> = {}) => ({
  ...as(session),
  "GET /bots": ok([BOT]),
  "GET /calls": ok({ items: [CALL], next_cursor: null }),
  [`GET /calls/${CALL_ID}`]: ok({ ...CALL, events: EVENTS }),
  ...extra,
});

test("the list shows each call and filters go to the API and the URL", async () => {
  const calls = fakeApi(api());
  const router = open("/admin/calls");

  const row = await screen.findByRole("row", { name: /Hvylia demo/ });
  expect(within(row).getByText("Acme")).toBeInTheDocument();
  expect(within(row).getByText("1:05")).toBeInTheDocument();
  expect(within(row).getByText("передано оператору")).toBeInTheDocument();

  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("Кінець"), "handoff");
  await user.selectOptions(screen.getByLabelText("Бот"), "b-1");

  await vi.waitFor(() => {
    const last = calls.filter((call) => call.key === "GET /calls").at(-1);
    expect(last?.query.get("outcome")).toBe("handoff");
    expect(last?.query.get("bot_id")).toBe("b-1");
  });
  expect(router.state.location.search).toBe("?outcome=handoff&bot=b-1");
});

test("more pages load with the cursor", async () => {
  const second = { ...CALL, id: "11111111-1111-1111-1111-111111111111", caller: "Globex" };
  const calls = fakeApi(
    api(ADMIN, {
      "GET /calls": ok({ items: [CALL], next_cursor: "c1" }),
    }),
  );
  open("/admin/calls");

  const user = userEvent.setup();
  await screen.findByRole("row", { name: /Acme/ });
  // The next page answers with the second call and no further cursor.
  vi.mocked(fetch).mockImplementationOnce(async (request) => {
    calls.push({ key: "GET /calls", body: undefined, csrf: null, query: new URL((request as Request).url).searchParams });
    return new Response(JSON.stringify({ items: [second], next_cursor: null }));
  });
  await user.click(screen.getByRole("button", { name: "Показати ще" }));

  expect(await screen.findByRole("row", { name: /Globex/ })).toBeInTheDocument();
  expect(calls.at(-1)?.query.get("cursor")).toBe("c1");
  expect(screen.queryByRole("button", { name: "Показати ще" })).not.toBeInTheDocument();
});

test("search by id opens the call; a wrong id is explained", async () => {
  fakeApi(api());
  const router = open("/admin/calls");

  const user = userEvent.setup();
  const search = await screen.findByRole("textbox", { name: "Id дзвінка" });
  await user.type(search, "not-an-id{Enter}");
  expect(screen.getByRole("alert")).toHaveTextContent("не схоже на id");

  await user.clear(search);
  await user.type(search, ` ${CALL_ID.toUpperCase()} {Enter}`);
  expect(await screen.findByRole("heading", { name: "Дзвінок" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe(`/admin/calls/${CALL_ID}`);
});

test("the timeline shows messages, nodes, tools, timings and errors in order", async () => {
  fakeApi(api(VIEWER));
  open(`/admin/calls/${CALL_ID}`);

  const timeline = await screen.findByRole("list", { name: "Таймлайн" });
  const items = within(timeline).getAllByRole("listitem");
  expect(items).toHaveLength(EVENTS.length);
  expect(items[1]).toHaveTextContent("+0:02Абонентмій баланс?");
  expect(items[2]).toHaveTextContent("вузол assist");
  expect(items[3]).toHaveTextContent("Виклик get_balance");
  expect(items[3]).toHaveTextContent('"msisdn": "380"');
  expect(items[4]).toHaveTextContent('"balance": 120');
  expect(items[5]).toHaveTextContent("БотНа рахунку 120 грн.");
  expect(items[6]).toHaveTextContent("Хід: 2.0 с · перший токен 850 мс");
  expect(items[6]).toHaveTextContent("llm_ttft");
  expect(items[6]).toHaveTextContent("300 мс");
  expect(items[7]).toHaveTextContent("Помилка tool_timeout: Billing is slow");
  expect(items[8]).toHaveTextContent("+1:05Кінець: передано оператору");
  expect(screen.getByText("Хто").nextElementSibling).toHaveTextContent("Acme");
});

test("an unknown call shows not found", async () => {
  fakeApi(api());
  open("/admin/calls/22222222-2222-2222-2222-222222222222");

  expect(await screen.findByRole("heading", { name: "Дзвінок не знайдено" })).toBeInTheDocument();
});

test("admins watch calls in progress; the list refreshes when one starts", async () => {
  const calls = fakeApi(api());
  open("/admin/calls");

  const panel = await screen.findByRole("region", { name: "Зараз в ефірі" });
  const ws = FakeWebSocket.instances.at(-1);
  expect(ws?.url).toBe(`ws://${location.host}/calls/live`);
  ws?.open();
  expect(panel).toHaveTextContent("наживо");
  expect(panel).toHaveTextContent("Активних дзвінків немає.");

  const before = calls.filter((call) => call.key === "GET /calls").length;
  const live = { id: "1-0", call_id: CALL_ID, bot_id: "b-1", payload: {} };
  ws?.push({ ...live, type: "session_started", ts: "2026-10-19T10:00:00Z" });
  ws?.push({ ...live, id: "2-0", type: "user_message", ts: "2026-10-19T10:00:02Z" });

  expect(within(panel).getByRole("link", { name: CALL_ID.slice(0, 8) })).toBeInTheDocument();
  expect(panel).toHaveTextContent("реплік: 1");
  await vi.waitFor(() => expect(calls.filter((call) => call.key === "GET /calls").length).toBeGreaterThan(before));

  ws?.push({ ...live, id: "3-0", type: "session_end", ts: "2026-10-19T10:01:00Z", payload: { reason: "completed" } });
  expect(panel).toHaveTextContent("Активних дзвінків немає.");
});

test("viewers get no live panel", async () => {
  fakeApi(api(VIEWER));
  open("/admin/calls");

  await screen.findByRole("row", { name: /Hvylia demo/ });
  expect(screen.queryByRole("region", { name: "Зараз в ефірі" })).not.toBeInTheDocument();
  expect(FakeWebSocket.instances).toHaveLength(0);
});
