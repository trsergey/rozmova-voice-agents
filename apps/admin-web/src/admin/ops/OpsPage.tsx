// "Операції" (FR-20): calls in progress and, for the last hour and day, p95 latency per
// stage and error rates. The API caches the summary for 10 s, so the page polls at that rate.
// The first-token p95 is red above the PRD target; the turn and first-token p95 count only
// turns that called the LLM (the API leaves greetings out).
import { useQuery } from "@tanstack/react-query";

import { client, unwrap, type Schemas } from "../../api/client";
import { formatMs, formatTime } from "../format";
import { Card, ErrorText, PageHeader } from "../ui";
import { DemoReset } from "./DemoReset";

type Window = Schemas["WindowStats"];

const REFRESH_MS = 10_000;
// PRD "Відповідати швидко": first token p95 under 1.5 s in text mode.
export const TTFT_TARGET_MS = 1500;
const WINDOW_TITLES: Record<Window["window"], string> = { "1h": "Остання година", "24h": "Остання доба" };
const P95_LABELS: [keyof Window["p95_ms"], string][] = [
  ["turn", "Хід повністю"],
  ["ttft", "До першого токена"],
  ["llm_ttft", "Перший токен LLM"],
  ["rag_search", "Пошук у базі знань"],
  ["tool", "Дія (виклик інструмента)"],
];

const percent = (rate: number | null | undefined) => (rate == null ? "—" : `${(rate * 100).toFixed(1)}%`);

export function OpsPage() {
  const summary = useQuery({
    queryKey: ["ops", "summary"],
    queryFn: () => unwrap(client.GET("/ops/summary")),
    refetchInterval: REFRESH_MS,
  });

  return (
    <>
      <PageHeader title="Операції">
        {summary.data && (
          <span className="text-sm text-fg-muted">оновлено о {formatTime(summary.data.generated_at)}</span>
        )}
      </PageHeader>
      {summary.isError && <ErrorText>Не вдалося завантажити зведення.</ErrorText>}
      {summary.data && (
        <div className="grid items-start gap-4 lg:grid-cols-[14rem_1fr_1fr]">
          <Card title="Зараз в ефірі">
            <p className="text-3xl font-semibold tabular-nums" aria-label="Активних дзвінків">
              {summary.data.active_calls ?? "—"}
            </p>
            <p className="mt-1 text-sm text-fg-muted">
              {summary.data.active_calls == null ? "Redis не відповідає" : "активних дзвінків"}
            </p>
          </Card>
          {summary.data.windows.map((window) => (
            <WindowCard key={window.window} window={window} />
          ))}
        </div>
      )}
      <div className="mt-6">
        <DemoReset />
      </div>
    </>
  );
}

function WindowCard({ window }: { window: Window }) {
  return (
    <section aria-label={WINDOW_TITLES[window.window]} className="rounded-lg border border-line bg-surface p-4">
      <h2 className="mb-3 text-sm font-medium text-fg-muted">{WINDOW_TITLES[window.window]}</h2>
      <dl className="mb-4 grid grid-cols-3 gap-2 text-center">
        <Stat label="Дзвінків" value={String(window.calls)} />
        <Stat label="Ходів" value={String(window.turns)} />
        <Stat label="Помилок на хід" value={percent(window.error_rate)} />
      </dl>
      <h3 className="mb-2 text-xs font-medium tracking-wide text-fg-muted uppercase">p95 затримки</h3>
      <dl className="grid grid-cols-[1fr_auto] gap-y-1 text-sm">
        {P95_LABELS.map(([key, label]) => {
          const value = window.p95_ms[key];
          const over = key === "ttft" && value != null && value > TTFT_TARGET_MS;
          return (
            <div key={key} className="contents">
              <dt className="text-fg-muted">
                {label}
                {key === "ttft" && <span className="text-xs"> (ціль &lt; {formatMs(TTFT_TARGET_MS)})</span>}
              </dt>
              <dd className={`text-right tabular-nums ${over ? "font-semibold text-danger" : ""}`} title={over ? "Вище цілі" : undefined}>
                {value == null ? "—" : formatMs(value)}
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="mt-1 text-xs text-fg-muted">Хід і перший токен: лише ходи з викликом моделі.</p>
      <p className="mt-4 text-sm">
        Помилки дій: <span className="font-medium tabular-nums">{percent(window.tool_error_rate)}</span>{" "}
        <span className="text-fg-muted">
          ({window.tool_errors} з {window.tool_calls})
        </span>
      </p>
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted-bg px-2 py-2">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  );
}
