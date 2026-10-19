// One call: who, which bot and version, how it ended, and its timeline.
import { Link, useParams } from "react-router";

import { ApiError } from "../../api/client";
import { formatDateTime, formatDuration } from "../format";
import { Badge, Card, PageHeader } from "../ui";
import { outcomeLabel } from "./outcomes";
import { useCall } from "./queries";
import { Timeline } from "./Timeline";

export function CallPage() {
  const { callId = "" } = useParams();
  const call = useCall(callId);

  const back = (
    <Link to="/admin/calls" className="mb-3 inline-block text-sm text-accent hover:underline">
      ← Усі дзвінки
    </Link>
  );
  if (call.isPending) return <p className="text-sm text-fg-muted">Завантаження…</p>;
  if (call.isError) {
    const missing = call.error instanceof ApiError && (call.error.status === 404 || call.error.status === 422);
    return (
      <>
        {back}
        <PageHeader title={missing ? "Дзвінок не знайдено" : "Помилка"} />
      </>
    );
  }
  const c = call.data;
  return (
    <>
      {back}
      <PageHeader title="Дзвінок">
        <code className="font-mono text-xs break-all text-fg-muted">{c.id}</code>
      </PageHeader>
      <div className="grid items-start gap-6 lg:grid-cols-[18rem_1fr]">
        <Card title="Підсумок">
          <dl className="grid grid-cols-[6rem_1fr] gap-y-2 text-sm">
            <dt className="text-fg-muted">Бот</dt>
            <dd>
              <Link to={`/admin/bots/${c.bot_id}`} className="text-accent hover:underline">
                {c.bot_name}
              </Link>
              {c.bot_version != null ? ` · v${c.bot_version}` : " · чернетка"}
            </dd>
            <dt className="text-fg-muted">Хто</dt>
            <dd>{c.caller ?? "—"}</dd>
            <dt className="text-fg-muted">Початок</dt>
            <dd>{formatDateTime(c.started_at)}</dd>
            <dt className="text-fg-muted">Тривалість</dt>
            <dd>{c.ended_at ? formatDuration(Date.parse(c.ended_at) - Date.parse(c.started_at)) : "—"}</dd>
            <dt className="text-fg-muted">Реплік</dt>
            <dd>{c.turns}</dd>
            <dt className="text-fg-muted">Кінець</dt>
            <dd>
              <Badge tone={c.outcome === "handoff" || c.outcome === null ? "neutral" : "ok"}>
                {outcomeLabel(c.outcome ?? null)}
              </Badge>
            </dd>
          </dl>
        </Card>
        <section>
          <h2 className="mb-3 text-sm font-medium text-fg-muted">Таймлайн</h2>
          <Timeline events={c.events} startedAt={c.started_at} />
        </section>
      </div>
    </>
  );
}
