// A call's events in order (FR-13, S5): what the caller and the bot said, which flow nodes
// ran, tool calls with their arguments and results, each turn's timings by stage, errors.
// Each row shows its offset from the call's start.
import type { ReactNode } from "react";

import { formatDuration, formatMs } from "../format";
import type { CallEvent } from "./queries";
import { outcomeLabel } from "./outcomes";

const str = (value: unknown): string => (typeof value === "string" ? value : "");
const json = (value: unknown): string => JSON.stringify(value, null, 2);

export function Timeline({ events, startedAt }: { events: CallEvent[]; startedAt: string }) {
  const start = new Date(startedAt).getTime();
  if (events.length === 0) return <p className="text-sm text-fg-muted">Подій немає.</p>;
  return (
    <ol className="grid gap-2" aria-label="Таймлайн">
      {events.map((event) => (
        <li key={event.id} className="grid grid-cols-[3.5rem_1fr] gap-3 text-sm">
          <span className="pt-1 font-mono text-xs text-fg-muted tabular-nums">
            +{formatDuration(new Date(event.ts).getTime() - start)}
          </span>
          <Row event={event} />
        </li>
      ))}
    </ol>
  );
}

function Row({ event }: { event: CallEvent }) {
  const p = event.payload;
  switch (event.type) {
    case "user_message":
      return <Bubble who="Абонент" tone="bg-accent/10">{str(p.text)}</Bubble>;
    case "assistant_message":
      return (
        <Bubble who="Бот" tone="bg-muted-bg">
          {str(p.text)}
          {p.interrupted === true && <span className="text-fg-muted"> (перервано)</span>}
        </Bubble>
      );
    case "node_enter":
      return (
        <p className="text-fg-muted">
          → вузол <code className="rounded bg-muted-bg px-1 font-mono text-fg">{str(p.node_id)}</code>
        </p>
      );
    case "tool_call":
      return (
        <Details summary={`Виклик ${str(p.name)}`}>
          <pre>{json(p.args)}</pre>
        </Details>
      );
    case "tool_result":
      return (
        <Details summary={`Результат ${str(p.name)}`}>
          <pre>{json(p.result)}</pre>
        </Details>
      );
    case "turn_end":
      return <Timings timings={p.timings} />;
    case "error":
      return (
        <p role="note" className="rounded-md bg-danger/10 px-3 py-2 text-danger">
          Помилка <code className="font-mono">{str(p.code)}</code>: {str(p.message)}
        </p>
      );
    case "handoff":
      return <p className="font-medium">Передано оператору: {str(p.reason)}</p>;
    case "session_started":
      return (
        <p className="text-fg-muted">
          {p.resumed === true ? "Відновлення з'єднання" : "Початок дзвінка"}
          {typeof p.bot_version === "number" ? ` · v${p.bot_version}` : " · чернетка"}
        </p>
      );
    case "session_end":
      return <p className="text-fg-muted">Кінець: {outcomeLabel(str(p.reason))}</p>;
    case "session_closed":
      return (
        <p className="text-fg-muted">
          З'єднання закрито ({String(p.code)}){p.resumable === true ? ", можна відновити" : ""}
        </p>
      );
    case "turn_cancelled":
      return <p className="text-fg-muted">Хід скасовано</p>;
    default:
      return (
        <Details summary={event.type}>
          <pre>{json(p)}</pre>
        </Details>
      );
  }
}

function Bubble({ who, tone, children }: { who: string; tone: string; children: ReactNode }) {
  return (
    <div className={`rounded-lg px-3 py-2 ${tone}`}>
      <p className="mb-0.5 text-xs font-medium text-fg-muted">{who}</p>
      <p className="whitespace-pre-wrap">{children}</p>
    </div>
  );
}

function Details({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="rounded-md border border-line bg-surface px-3 py-1.5">
      <summary className="cursor-pointer font-mono text-xs">{summary}</summary>
      <div className="mt-2 overflow-x-auto font-mono text-xs">{children}</div>
    </details>
  );
}

function Timings({ timings }: { timings: unknown }) {
  const t = (timings ?? {}) as { total_ms?: number; ttft_ms?: number | null; stages?: Record<string, number> };
  const total = t.total_ms ?? 0;
  const stages = Object.entries(t.stages ?? {});
  return (
    <div className="rounded-md border border-line bg-surface px-3 py-2">
      <p className="text-xs text-fg-muted">
        Хід: <span className="font-medium text-fg">{formatMs(total)}</span>
        {t.ttft_ms != null && <> · перший токен {formatMs(t.ttft_ms)}</>}
      </p>
      {stages.length > 0 && (
        <dl className="mt-2 grid grid-cols-[8rem_1fr_4rem] items-center gap-x-2 gap-y-1 text-xs">
          {stages.map(([name, ms]) => (
            <div key={name} className="contents">
              <dt className="truncate font-mono text-fg-muted">{name}</dt>
              <dd className="h-1.5 rounded bg-muted-bg">
                <div className="h-1.5 rounded bg-accent" style={{ width: `${total ? Math.min(100, (ms / total) * 100) : 0}%` }} />
              </dd>
              <dd className="text-right tabular-nums">{formatMs(ms)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
