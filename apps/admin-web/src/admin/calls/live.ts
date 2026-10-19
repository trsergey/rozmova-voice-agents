// The live calls panel's state, folded from `WS /calls/live` events: calls in progress by id,
// newest first. A call leaves on session_end, or on session_closed that cannot resume.
export interface LiveEvent {
  id: string;
  call_id: string;
  bot_id: string;
  type: string;
  ts: string;
  payload: Record<string, unknown>;
}

export interface LiveCall {
  callId: string;
  botId: string;
  startedAt: string;
  lastType: string;
  lastAt: string;
  turns: number;
}

export type LiveCalls = LiveCall[];

const MAX_CALLS = 50;

export function liveReducer(calls: LiveCalls, event: LiveEvent): LiveCalls {
  const ended =
    event.type === "session_end" || (event.type === "session_closed" && event.payload.resumable !== true);
  const current = calls.find((call) => call.callId === event.call_id);
  const rest = calls.filter((call) => call.callId !== event.call_id);
  if (ended) return rest;
  const next: LiveCall = {
    callId: event.call_id,
    botId: event.bot_id,
    startedAt: current?.startedAt ?? event.ts,
    lastType: event.type,
    lastAt: event.ts,
    turns: (current?.turns ?? 0) + (event.type === "user_message" ? 1 : 0),
  };
  return [next, ...rest].slice(0, MAX_CALLS);
}

export function parseLiveEvent(data: string): LiveEvent | null {
  try {
    const event = JSON.parse(data) as Partial<LiveEvent>;
    return typeof event.call_id === "string" && typeof event.type === "string" && typeof event.ts === "string"
      ? (event as LiveEvent)
      : null;
  } catch {
    return null;
  }
}
