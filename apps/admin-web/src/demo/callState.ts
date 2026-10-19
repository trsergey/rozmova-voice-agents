// What the chat shows, as a pure function of the server events (docs/protocol.md). The
// WebSocket handling lives in useCall; this file has no I/O so the tests drive it directly.
import type { ServerMessage, Source } from "../../../../packages/contracts/ts/protocol.server";

export type Phase = "idle" | "connecting" | "live" | "reconnecting" | "ended";

export interface UserLine {
  role: "user";
  text: string;
}

export interface BotLine {
  role: "assistant";
  // null for lines restored from a resumed session's history.
  turnId: number | null;
  text: string;
  sources: Source[];
  interrupted: boolean;
  done: boolean;
}

export type Line = UserLine | BotLine;

export interface CallState {
  phase: Phase;
  callId: string | null;
  lines: Line[];
  // A transient problem with the last utterance; the call goes on.
  error: string | null;
  // Why the call ended, shown under the conversation.
  ended: string | null;
}

export type CallAction =
  | { type: "connecting"; callId: string | null }
  | { type: "server"; message: ServerMessage }
  | { type: "user_text"; text: string }
  | { type: "reconnecting" }
  | { type: "failed"; message: string };

export const initialCallState: CallState = { phase: "idle", callId: null, lines: [], error: null, ended: null };

const END_REASONS: Record<string, string> = {
  hangup: "Розмову завершено.",
  completed: "Розмову завершено.",
  timeout: "Розмову завершено через неактивність.",
  limit: "Досягнуто ліміту демо: 10 хвилин або 30 реплік на розмову, до 3 розмов одночасно.",
  handoff: "Розмову передано оператору. У демо на цьому вона завершується.",
};

const ERRORS: Record<string, string> = {
  rate_limited: "Забагато повідомлень. Зачекайте хвилину й спробуйте ще раз.",
  turn_timeout: "Асистент не встиг відповісти. Спробуйте ще раз.",
  llm_unavailable: "Асистент зараз недоступний. Спробуйте ще раз за хвилину.",
  tool_failed: "Не вдалося виконати дію. Спробуйте ще раз.",
  kb_unavailable: "База знань тимчасово недоступна.",
};
const DEFAULT_ERROR = "Щось пішло не так. Спробуйте ще раз.";

function updateTurn(lines: Line[], turnId: number, update: (line: BotLine) => BotLine): Line[] {
  const index = lines.findIndex((line) => line.role === "assistant" && line.turnId === turnId);
  if (index === -1) return lines;
  return lines.map((line, i) => (i === index ? update(line as BotLine) : line));
}

function onServer(state: CallState, message: ServerMessage): CallState {
  switch (message.type) {
    case "session_started": {
      const lines: Line[] = (message.history ?? []).map((item) =>
        item.role === "user"
          ? { role: "user", text: item.text }
          : { role: "assistant", turnId: null, text: item.text, sources: [], interrupted: item.interrupted, done: true },
      );
      return {
        ...state,
        phase: "live",
        callId: message.call_id,
        lines: message.resumed ? lines : state.lines,
        error: null,
      };
    }
    case "assistant_delta": {
      const exists = state.lines.some((line) => line.role === "assistant" && line.turnId === message.turn_id);
      const lines = exists
        ? updateTurn(state.lines, message.turn_id, (line) => ({ ...line, text: line.text + message.text }))
        : [
            ...state.lines,
            { role: "assistant" as const, turnId: message.turn_id, text: message.text, sources: [], interrupted: false, done: false },
          ];
      return { ...state, lines };
    }
    case "turn_end":
      return {
        ...state,
        lines: updateTurn(state.lines, message.turn_id, (line) => ({ ...line, sources: message.sources, done: true })),
      };
    case "turn_cancelled":
      return {
        ...state,
        lines: updateTurn(state.lines, message.turn_id, (line) => ({ ...line, interrupted: true, done: true })),
      };
    case "error":
      return { ...state, error: ERRORS[message.code] ?? DEFAULT_ERROR };
    case "handoff":
      return { ...state, ended: END_REASONS.handoff ?? null };
    case "session_end":
      // A gateway restart: useCall reconnects with the same call_id.
      if (message.reason === "shutdown") return { ...state, phase: "reconnecting" };
      return { ...state, phase: "ended", ended: state.ended ?? END_REASONS[message.reason] ?? END_REASONS.hangup ?? null };
    default:
      // Debug events (tool_call, tool_result, node_enter) are not sent to viewers; ignore them.
      return state;
  }
}

export function callReducer(state: CallState, action: CallAction): CallState {
  switch (action.type) {
    case "connecting":
      return action.callId
        ? { ...state, phase: "connecting", ended: null }
        : { ...initialCallState, phase: "connecting" };
    case "reconnecting":
      return { ...state, phase: "reconnecting" };
    case "user_text":
      return { ...state, error: null, lines: [...state.lines, { role: "user", text: action.text }] };
    case "failed":
      return { ...state, phase: "ended", ended: action.message };
    case "server":
      return onServer(state, action.message);
  }
}
