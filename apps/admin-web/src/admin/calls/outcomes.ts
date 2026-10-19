// How a call ended, in words: the gateway's session_end reasons and `open` for none yet.
import type { Outcome } from "./queries";

export const OUTCOMES: Record<Outcome, string> = {
  completed: "завершено ботом",
  hangup: "абонент завершив",
  handoff: "передано оператору",
  timeout: "тайм-аут",
  limit: "ліміт",
  shutdown: "перезапуск сервера",
  open: "без завершення",
};

export function outcomeLabel(outcome: string | null): string {
  return outcome === null ? OUTCOMES.open : (OUTCOMES[outcome as Outcome] ?? outcome);
}
