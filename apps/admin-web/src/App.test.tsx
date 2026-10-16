// The whole page against a fake admin API (fetch) and a fake gateway (WebSocket).
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ServerMessage } from "../../../packages/contracts/ts/protocol.server";
import { App } from "./App";

const CALL_ID = "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c";
const BOT = { id: "bot-1", name: "Hvylia demo", live_version_id: "v-1", live_version: 2 };
const SESSION = { user: { id: "code-1", name: "demo", role: "viewer" }, csrf_token: "csrf-1" };

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readyState = FakeWebSocket.OPEN;
  sent: unknown[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(JSON.parse(data));
  }

  close() {}

  receive(...messages: ServerMessage[]) {
    act(() => messages.forEach((message) => this.onmessage?.({ data: JSON.stringify(message) })));
  }

  drop(code: number) {
    act(() => this.onclose?.({ code }));
  }
}

type Route = (body: unknown) => { status: number; body?: unknown };

function fakeApi(routes: Record<string, Route>) {
  const calls: { key: string; body: unknown; csrf: string | null }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      const key = `${init.method} ${path}`;
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      calls.push({ key, body, csrf: (init.headers as Record<string, string>)["X-CSRF-Token"] ?? null });
      const route = routes[key];
      const { status, body: out } = route ? route(body) : { status: 404, body: { error: { code: "not_found", message: "x" } } };
      return new Response(out === undefined ? null : JSON.stringify(out), { status });
    }),
  );
  return calls;
}

const ticket = (body: unknown) => {
  const callId = (body as { call_id: string | null }).call_id ?? CALL_ID;
  return { status: 200, body: { ticket: "t", call_id: callId, ws_url: `wss://gw/ws/calls/bot-1?call_id=${callId}` } };
};

const loggedIn = {
  "GET /auth/me": () => ({ status: 200, body: SESSION }),
  "GET /bots": () => ({ status: 200, body: [{ ...BOT, id: "draft-only", live_version_id: null }, BOT] }),
  "POST /calls/ticket": ticket,
};

const greeting: ServerMessage[] = [
  { type: "session_started", call_id: CALL_ID, bot_version: 2, resumed: false, history: null },
  { type: "assistant_delta", turn_id: 1, text: "Вітаю! Чим можу допомогти?" },
  { type: "turn_end", turn_id: 1, timings: { total_ms: 5, ttft_ms: 2, stages: {} }, sources: [] },
];

async function startCall() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "Почати розмову" }));
  const ws = FakeWebSocket.instances.at(-1);
  if (!ws) throw new Error("no WebSocket opened");
  ws.receive(...greeting);
  return { user, ws };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("an access code logs in and opens the chat with the published bot", async () => {
  let loggedInYet = false;
  const calls = fakeApi({
    "GET /auth/me": () => (loggedInYet ? { status: 200, body: SESSION } : { status: 401, body: { error: { code: "not_authenticated", message: "x" } } }),
    "POST /auth/access-code": (body) => {
      if ((body as { code: string }).code !== "DEMO-1234") return { status: 401, body: { error: { code: "invalid_access_code", message: "x" } } };
      loggedInYet = true;
      return { status: 200, body: SESSION };
    },
    "GET /bots": loggedIn["GET /bots"],
  });
  const user = userEvent.setup();
  render(<App />);

  await user.type(await screen.findByLabelText("Код доступу"), "WRONG");
  await user.click(screen.getByRole("button", { name: "Увійти" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Код недійсний");

  await user.clear(screen.getByLabelText("Код доступу"));
  await user.type(screen.getByLabelText("Код доступу"), "DEMO-1234");
  await user.click(screen.getByRole("button", { name: "Увійти" }));

  expect(await screen.findByText("Hvylia demo")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Почати розмову" })).toBeInTheDocument();
  expect(calls.map((call) => call.key)).toContain("POST /auth/access-code");
});

test("a call streams the reply, lists the cited sources and sends no debug flag", async () => {
  const calls = fakeApi(loggedIn);
  const { user, ws } = await startCall();

  expect(ws.url).toContain(`call_id=${CALL_ID}`);
  const ticketCall = calls.find((call) => call.key === "POST /calls/ticket");
  expect(ticketCall?.body).toEqual({ bot_id: "bot-1", call_id: null });
  expect(ticketCall?.csrf).toBe("csrf-1");
  expect(screen.getByText("Вітаю! Чим можу допомогти?")).toBeInTheDocument();

  await user.type(screen.getByLabelText("Повідомлення"), "як перевірити баланс?{Enter}");
  expect(ws.sent).toEqual([{ type: "user_text", text: "як перевірити баланс?" }]);
  expect(screen.getByText("як перевірити баланс?")).toBeInTheDocument();

  ws.receive({ type: "assistant_delta", turn_id: 2, text: "Наберіть *111# " });
  ws.receive({ type: "assistant_delta", turn_id: 2, text: "[1]." });
  ws.receive({
    type: "turn_end",
    turn_id: 2,
    timings: { total_ms: 5, ttft_ms: 2, stages: {} },
    sources: [{ marker: 1, chunk_id: "faq-1", document_id: "faq", document_title: "Часті питання", section: "Баланс" }],
  });

  expect(screen.getByText("Наберіть *111# [1].")).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Джерела" })).toHaveTextContent("Часті питання — Баланс");
});

test("a handoff shows the notice and offers a new call", async () => {
  fakeApi(loggedIn);
  const { ws } = await startCall();

  ws.receive({ type: "handoff", reason: "requested_or_unresolved" }, { type: "session_end", reason: "handoff" });
  ws.drop(1000);

  expect(screen.getByText(/Розмову передано оператору/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Нова розмова" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Повідомлення")).not.toBeInTheDocument();
});

test("hangup sends hangup and the call ends", async () => {
  fakeApi(loggedIn);
  const { user, ws } = await startCall();

  await user.click(screen.getByRole("button", { name: "Завершити" }));
  expect(ws.sent).toEqual([{ type: "hangup" }]);

  ws.receive({ type: "session_end", reason: "hangup" });
  ws.drop(1000);
  expect(screen.getByText("Розмову завершено.")).toBeInTheDocument();
});

test("a gateway restart reconnects with the same call_id and resends the unanswered utterance", async () => {
  const calls = fakeApi(loggedIn);
  const { user, ws } = await startCall();
  await user.type(screen.getByLabelText("Повідомлення"), "мій баланс?{Enter}");

  vi.useFakeTimers();
  ws.receive({ type: "session_end", reason: "shutdown" });
  ws.drop(1012);
  expect(screen.getByText("Відновлюємо з'єднання…")).toBeInTheDocument();
  await act(() => vi.advanceTimersByTimeAsync(1000));
  vi.useRealTimers();

  const ticketBodies = calls.filter((call) => call.key === "POST /calls/ticket").map((call) => call.body);
  expect(ticketBodies).toEqual([
    { bot_id: "bot-1", call_id: null },
    { bot_id: "bot-1", call_id: CALL_ID },
  ]);
  const resumed = FakeWebSocket.instances.at(-1);
  expect(resumed).not.toBe(ws);
  resumed?.receive({
    type: "session_started",
    call_id: CALL_ID,
    bot_version: 2,
    resumed: true,
    history: [{ role: "assistant", text: "Вітаю! Чим можу допомогти?", interrupted: false }],
  });

  expect(resumed?.sent).toEqual([{ type: "user_text", text: "мій баланс?" }]);
  expect(screen.getByText("мій баланс?")).toBeInTheDocument();
  expect(screen.getByLabelText("Повідомлення")).toBeEnabled();
});
