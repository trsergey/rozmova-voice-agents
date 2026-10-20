// How a call ended, in words: the gateway's session_end reasons, `abandoned` for a call the
// subscriber left without ending (closed by the call log), and `open` for none yet.
import type { Outcome } from "./queries";

export const OUTCOMES: Record<Outcome, string> = {
  completed: "завершено ботом",
  hangup: "абонент завершив",
  handoff: "передано оператору",
  timeout: "тайм-аут",
  limit: "ліміт",
  shutdown: "перезапуск сервера",
  abandoned: "з'єднання втрачено",
  open: "без завершення",
};

export function outcomeLabel(outcome: string | null): string {
  return outcome === null ? OUTCOMES.open : (OUTCOMES[outcome as Outcome] ?? outcome);
}
