import { liveReducer, parseLiveEvent, type LiveCalls, type LiveEvent } from "./live";

const event = (call_id: string, type: string, ts: string, payload: Record<string, unknown> = {}): LiveEvent => ({
  id: `${ts}-0`,
  call_id,
  bot_id: "b-1",
  type,
  ts,
  payload,
});

const fold = (events: LiveEvent[]): LiveCalls => events.reduce(liveReducer, []);

test("a call appears on its first event, counts turns and moves to the top", () => {
  const calls = fold([
    event("a", "session_started", "t1"),
    event("b", "session_started", "t2"),
    event("a", "user_message", "t3"),
    event("a", "user_message", "t4"),
  ]);

  expect(calls.map((call) => call.callId)).toEqual(["a", "b"]);
  expect(calls[0]).toMatchObject({ startedAt: "t1", lastType: "user_message", lastAt: "t4", turns: 2 });
});

test("a call leaves on session_end or a close it cannot resume from", () => {
  const calls = fold([
    event("a", "session_started", "t1"),
    event("b", "session_started", "t2"),
    event("c", "session_started", "t3"),
    event("a", "session_end", "t4", { reason: "completed" }),
    event("b", "session_closed", "t5", { code: 1006, resumable: true }),
    event("c", "session_closed", "t6", { code: 1000, resumable: false }),
  ]);

  expect(calls.map((call) => call.callId)).toEqual(["b"]);
});

test("malformed messages are ignored", () => {
  expect(parseLiveEvent("not json")).toBeNull();
  expect(parseLiveEvent(JSON.stringify({ type: "x" }))).toBeNull();
  expect(parseLiveEvent(JSON.stringify(event("a", "node_enter", "t1")))?.call_id).toBe("a");
});
