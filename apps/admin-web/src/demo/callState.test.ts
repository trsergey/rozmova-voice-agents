import type { ServerMessage } from "../../../../packages/contracts/ts/protocol.server";
import { callReducer, initialCallState, type CallAction, type CallState } from "./callState";

const CALL_ID = "3f2c1b9e-8d4a-4c1e-9b7a-2e5d6f8a9b0c";

function run(...actions: CallAction[]): CallState {
  return actions.reduce(callReducer, initialCallState);
}

const server = (message: ServerMessage): CallAction => ({ type: "server", message });
const started = server({ type: "session_started", call_id: CALL_ID, bot_version: 2, resumed: false, history: null });
const delta = (turn_id: number, text: string) => server({ type: "assistant_delta", turn_id, text });
const turnEnd = (turn_id: number, sources: Extract<ServerMessage, { type: "turn_end" }>["sources"] = []) =>
  server({ type: "turn_end", turn_id, timings: { total_ms: 10, ttft_ms: 5, stages: {} }, sources });

test("deltas of one turn build one reply; turn_end attaches the sources", () => {
  const source = { marker: 1, chunk_id: "faq-1", document_id: "faq", document_title: "FAQ", section: "Баланс" };
  const state = run(
    { type: "connecting", callId: null },
    started,
    delta(1, "Вітаю! "),
    delta(1, "Чим допомогти?"),
    turnEnd(1),
    { type: "user_text", text: "як перевірити баланс?" },
    delta(2, "Наберіть *111# [1]."),
    turnEnd(2, [source]),
  );

  expect(state.phase).toBe("live");
  expect(state.callId).toBe(CALL_ID);
  expect(state.lines).toEqual([
    { role: "assistant", turnId: 1, text: "Вітаю! Чим допомогти?", sources: [], interrupted: false, done: true },
    { role: "user", text: "як перевірити баланс?" },
    { role: "assistant", turnId: 2, text: "Наберіть *111# [1].", sources: [source], interrupted: false, done: true },
  ]);
});

test("a cancelled turn keeps its partial text, marked interrupted", () => {
  const state = run(started, delta(2, "Тариф Безлім"), server({ type: "turn_cancelled", turn_id: 2 }));
  expect(state.lines[0]).toMatchObject({ text: "Тариф Безлім", interrupted: true, done: true });
});

test("a handoff ends the call with the handoff notice", () => {
  const state = run(
    started,
    server({ type: "handoff", reason: "requested_or_unresolved" }),
    server({ type: "session_end", reason: "handoff" }),
  );
  expect(state.phase).toBe("ended");
  expect(state.ended).toMatch(/оператору/);
});

test("a limit before session_started ends the call with the limit notice", () => {
  const state = run({ type: "connecting", callId: null }, server({ type: "session_end", reason: "limit" }));
  expect(state.phase).toBe("ended");
  expect(state.ended).toMatch(/ліміту/);
});

test("a gateway restart is a reconnect, and the resumed history replaces the lines", () => {
  const restarted = run(started, delta(1, "Вітаю!"), turnEnd(1), server({ type: "session_end", reason: "shutdown" }));
  expect(restarted.phase).toBe("reconnecting");
  expect(restarted.ended).toBeNull();

  const resumed = run(
    { type: "connecting", callId: CALL_ID },
    server({
      type: "session_started",
      call_id: CALL_ID,
      bot_version: 2,
      resumed: true,
      history: [
        { role: "assistant", text: "Вітаю!", interrupted: false },
        { role: "user", text: "мій баланс?", interrupted: false },
      ],
    }),
  );
  expect(resumed.phase).toBe("live");
  expect(resumed.lines).toEqual([
    { role: "assistant", turnId: null, text: "Вітаю!", sources: [], interrupted: false, done: true },
    { role: "user", text: "мій баланс?" },
  ]);
});

test("errors are shown in Ukrainian and cleared by the next utterance", () => {
  const limited = run(started, server({ type: "error", code: "rate_limited", message: "x", retryable: true }));
  expect(limited.error).toMatch(/Забагато повідомлень/);
  expect(callReducer(limited, { type: "user_text", text: "ще раз" }).error).toBeNull();

  const unknown = run(started, server({ type: "error", code: "internal", message: "x", retryable: false }));
  expect(unknown.error).toMatch(/Щось пішло не так/);
});

test("debug events change nothing", () => {
  const before = run(started);
  const after = callReducer(before, server({ type: "node_enter", node_id: "assist" }));
  expect(after).toBe(before);
});
