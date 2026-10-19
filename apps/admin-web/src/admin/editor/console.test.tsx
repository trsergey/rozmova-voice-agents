// The editor's test console against a fake admin API and a fake gateway WebSocket.
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ServerMessage } from "../../../../../packages/contracts/ts/protocol.server";
import demo from "../../../../../flows/hvylia-demo.json";
import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";

const CALL_ID = "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c";
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
const ok = (body: unknown): Route => () => ({ status: 200, body });

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readyState = FakeWebSocket.OPEN;
  sent: unknown[] = [];
  closed = false;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(JSON.parse(data));
  }

  close() {
    this.closed = true;
  }

  receive(...messages: ServerMessage[]) {
    act(() => messages.forEach((message) => this.onmessage?.({ data: JSON.stringify(message) })));
  }
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  vi.stubGlobal("DOMMatrixReadOnly", class {
    m22 = 1;
  });
});
afterEach(() => vi.unstubAllGlobals());

const ticket = (body: unknown) => {
  const callId = (body as { call_id: string | null }).call_id ?? CALL_ID;
  return { status: 200, body: { ticket: "t", call_id: callId, ws_url: `wss://gw/ws/calls/b-1?call_id=${callId}&ticket=t` } };
};

const api = (session = ADMIN) => ({
  ...as(session),
  "GET /bots/b-1": ok(BOT),
  "GET /bots/b-1/draft": ok({ flow: demo, updated_at: null, hash: "h" }),
  "POST /bots/b-1/validate": ok({ errors: [], warnings: [] }),
  "PUT /bots/b-1/draft": (body: unknown) => ({ status: 200, body: { flow: body, updated_at: "2026-10-19T10:00:00Z", hash: "h2" } }),
  "POST /calls/ticket": ticket,
});

async function openConsole() {
  const calls = fakeApi(api());
  open("/admin/bots/b-1/editor");
  await screen.findByTestId("node-assist");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Тестувати" }));
  const panel = screen.getByRole("complementary", { name: "Тестова консоль" });
  return { calls, user, panel };
}

async function startCall(user: ReturnType<typeof userEvent.setup>, panel: HTMLElement) {
  await user.click(within(panel).getByRole("button", { name: "Почати" }));
  await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  const ws = FakeWebSocket.instances[0] as FakeWebSocket;
  ws.receive({ type: "session_started", call_id: CALL_ID, bot_version: null, resumed: false, history: null });
  return ws;
}

test("the console runs the saved draft on a debug call", async () => {
  const { calls, user, panel } = await openConsole();
  const ws = await startCall(user, panel);

  expect(calls.find((call) => call.key === "POST /calls/ticket")?.body).toEqual({ bot_id: "b-1", call_id: null, debug: true });
  expect(ws.url).toBe(`wss://gw/ws/calls/b-1?call_id=${CALL_ID}&ticket=t&draft=true`);
  expect(panel).toHaveTextContent("триває");
});

test("the active node is highlighted on the canvas as the call moves", async () => {
  const { user, panel } = await openConsole();
  const ws = await startCall(user, panel);

  ws.receive({ type: "node_enter", node_id: "start" });
  expect(screen.getByTestId("node-start")).toHaveAttribute("aria-current", "step");
  ws.receive({ type: "node_enter", node_id: "assist" });
  expect(screen.getByTestId("node-assist")).toHaveAttribute("aria-current", "step");
  expect(screen.getByTestId("node-start")).not.toHaveAttribute("aria-current");

  await user.click(screen.getByRole("button", { name: "Сховати тест" }));
  expect(screen.getByTestId("node-assist")).not.toHaveAttribute("aria-current");
  expect(ws.sent).toContainEqual({ type: "hangup" });
});

test("replies stream in, with the tools the bot called and the sources it cited", async () => {
  const { user, panel } = await openConsole();
  const ws = await startCall(user, panel);

  await user.type(within(panel).getByRole("textbox", { name: "Репліка абонента" }), "мій баланс?{Enter}");
  expect(ws.sent).toContainEqual({ type: "user_text", text: "мій баланс?" });

  ws.receive(
    { type: "tool_call", turn_id: 1, name: "get_balance", args: { msisdn: "380" } },
    { type: "assistant_delta", turn_id: 1, text: "На рахунку " },
  );
  const talk = within(panel).getByRole("list", { name: "Розмова" });
  expect(talk).toHaveTextContent("⚙ get_balance · виконується…");
  expect(talk).toHaveTextContent("На рахунку ▍");

  ws.receive(
    { type: "tool_result", turn_id: 1, name: "get_balance", result: { balance: 120 } },
    { type: "assistant_delta", turn_id: 1, text: "120 грн [1]." },
    {
      type: "turn_end",
      turn_id: 1,
      timings: { total_ms: 900, ttft_ms: 400, stages: {} },
      sources: [{ marker: 1, chunk_id: "c", document_id: "d", document_title: "Тарифи", section: "Ціни" }],
    },
  );
  expect(talk).toHaveTextContent("get_balance · готово");
  expect(talk).toHaveTextContent('"balance": 120');
  expect(talk).toHaveTextContent("На рахунку 120 грн [1].");
  expect(within(talk).getByRole("list", { name: "Джерела" })).toHaveTextContent("Тарифи — Ціни");
});

test("restart starts a new call and ignores the old one", async () => {
  const { calls, user, panel } = await openConsole();
  const first = await startCall(user, panel);
  first.receive({ type: "assistant_delta", turn_id: 1, text: "Вітаю!" });

  await user.click(within(panel).getByRole("button", { name: "Почати заново" }));
  await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));

  expect(first.closed).toBe(true);
  first.receive({ type: "assistant_delta", turn_id: 2, text: "запізніле" });
  expect(panel).not.toHaveTextContent("запізніле");
  expect(panel).not.toHaveTextContent("Вітаю!");
  expect(calls.filter((call) => call.key === "POST /calls/ticket").map((call) => call.body)).toEqual([
    { bot_id: "b-1", call_id: null, debug: true },
    { bot_id: "b-1", call_id: null, debug: true },
  ]);
});

test("unsaved changes are saved before a test when asked", async () => {
  const { calls, user, panel } = await openConsole();

  await user.click(within(screen.getByRole("complementary", { name: "Палітра" })).getByRole("button", { name: /Кінець/ }));
  expect(panel).toHaveTextContent("Тест іде на збереженій чернетці");
  await user.click(within(panel).getByRole("button", { name: "Зберегти й почати" }));

  await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  const keys = calls.map((call) => call.key);
  expect(keys.indexOf("PUT /bots/b-1/draft")).toBeLessThan(keys.indexOf("POST /calls/ticket"));
  expect(panel).not.toHaveTextContent("Тест іде на збереженій чернетці");
});

test("viewers have no test console", async () => {
  fakeApi({ ...api(VIEWER) });
  open("/admin/bots/b-1/editor");

  await screen.findByTestId("node-assist");
  expect(screen.queryByRole("button", { name: "Тестувати" })).not.toBeInTheDocument();
});
