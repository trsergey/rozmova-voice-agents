// The call log (FR-13): find a call by id, filter the list by bot, outcome and date, and,
// for admins, watch calls in progress. Filters live in the URL, so a filtered view is a link.
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { formatDateTime, formatDuration, formatTime } from "../format";
import { AdminOnly } from "../guards";
import { useBots } from "../bots/queries";
import { Badge, Button, ErrorText, Input, PageHeader, Select } from "../ui";
import { OUTCOMES, outcomeLabel } from "./outcomes";
import { useCalls, type Filters, type Outcome } from "./queries";
import { useLiveCalls, type LiveStatus } from "./useLiveCalls";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// A date input's day, as the start of that day in the viewer's time zone.
const dayStart = (day: string) => new Date(`${day}T00:00:00`).toISOString();
const nextDayStart = (day: string) => {
  const date = new Date(`${day}T00:00:00`);
  date.setDate(date.getDate() + 1);
  return date.toISOString();
};

export function CallsPage() {
  const [params, setParams] = useSearchParams();
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const filters: Filters = {
    bot_id: params.get("bot") || undefined,
    outcome: (params.get("outcome") as Outcome | null) || undefined,
    since: from ? dayStart(from) : undefined,
    until: to ? nextDayStart(to) : undefined,
  };

  const setParam = (key: string, value: string) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );

  return (
    <>
      <PageHeader title="Дзвінки">
        <SearchById />
      </PageHeader>
      <AdminOnly>
        <LivePanel />
      </AdminOnly>
      <Toolbar
        bot={params.get("bot") ?? ""}
        outcome={params.get("outcome") ?? ""}
        from={from}
        to={to}
        onChange={setParam}
      />
      <CallsTable filters={filters} />
    </>
  );
}

function SearchById() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [invalid, setInvalid] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const id = value.trim();
    if (!UUID.test(id)) {
      setInvalid(true);
      return;
    }
    navigate(`/admin/calls/${id.toLowerCase()}`);
  };

  return (
    <form onSubmit={submit} className="flex w-full max-w-md gap-2 sm:w-auto" role="search">
      <div className="grid flex-1 gap-1">
        <Input
          aria-label="Id дзвінка"
          placeholder="Id дзвінка"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setInvalid(false);
          }}
          className="font-mono sm:w-80"
        />
        {invalid && <ErrorText>Це не схоже на id дзвінка.</ErrorText>}
      </div>
      <Button type="submit" variant="secondary">
        Знайти
      </Button>
    </form>
  );
}

function Toolbar(props: { bot: string; outcome: string; from: string; to: string; onChange: (key: string, value: string) => void }) {
  const bots = useBots();
  const { onChange } = props;
  return (
    <div className="mb-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <Select aria-label="Бот" value={props.bot} onChange={(e) => onChange("bot", e.target.value)}>
        <option value="">Усі боти</option>
        {bots.data?.map((bot) => (
          <option key={bot.id} value={bot.id}>
            {bot.name}
          </option>
        ))}
      </Select>
      <Select aria-label="Кінець" value={props.outcome} onChange={(e) => onChange("outcome", e.target.value)}>
        <option value="">Будь-який кінець</option>
        {Object.entries(OUTCOMES).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </Select>
      <Input type="date" aria-label="З дати" value={props.from} onChange={(e) => onChange("from", e.target.value)} />
      <Input type="date" aria-label="По дату" value={props.to} onChange={(e) => onChange("to", e.target.value)} />
    </div>
  );
}

function CallsTable({ filters }: { filters: Filters }) {
  const calls = useCalls(filters);
  const rows = calls.data?.pages.flatMap((page) => page.items) ?? [];

  if (calls.isPending) return <p className="text-sm text-fg-muted">Завантаження…</p>;
  if (calls.isError) return <ErrorText>Не вдалося завантажити дзвінки.</ErrorText>;
  if (rows.length === 0) return <p className="text-sm text-fg-muted">Дзвінків не знайдено.</p>;
  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-line text-fg-muted">
            <tr>
              <th className="px-4 py-2 font-medium">Початок</th>
              <th className="px-4 py-2 font-medium">Бот</th>
              <th className="px-4 py-2 font-medium">Хто</th>
              <th className="px-4 py-2 font-medium">Реплік</th>
              <th className="px-4 py-2 font-medium">Тривалість</th>
              <th className="px-4 py-2 font-medium">Кінець</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((call) => (
              <tr key={call.id} className="border-b border-line last:border-b-0 hover:bg-muted-bg">
                <td className="px-4 py-3 whitespace-nowrap">
                  <Link to={call.id} className="text-accent hover:underline">
                    {formatDateTime(call.started_at)}
                  </Link>
                </td>
                <td className="px-4 py-3">
                  {call.bot_name}
                  <span className="text-fg-muted">{call.bot_version != null ? ` · v${call.bot_version}` : " · чернетка"}</span>
                </td>
                <td className="px-4 py-3 text-fg-muted">{call.caller ?? "—"}</td>
                <td className="px-4 py-3 tabular-nums">{call.turns}</td>
                <td className="px-4 py-3 tabular-nums">
                  {call.ended_at ? formatDuration(Date.parse(call.ended_at) - Date.parse(call.started_at)) : "—"}
                </td>
                <td className="px-4 py-3">
                  <Badge tone="neutral">{outcomeLabel(call.outcome ?? null)}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {calls.hasNextPage && (
        <div className="mt-4">
          <Button variant="secondary" disabled={calls.isFetchingNextPage} onClick={() => calls.fetchNextPage()}>
            Показати ще
          </Button>
        </div>
      )}
    </>
  );
}

const STATUS: Record<LiveStatus, string> = {
  connecting: "підключення…",
  live: "наживо",
  reconnecting: "перепідключення…",
  closed: "недоступно",
};

function LivePanel() {
  const { calls, status } = useLiveCalls();
  const bots = useBots();
  const botName = (id: string) => bots.data?.find((bot) => bot.id === id)?.name ?? "бот";

  return (
    <section aria-label="Зараз в ефірі" className="mb-6 rounded-lg border border-line bg-surface p-4">
      <h2 className="mb-2 flex items-center gap-2 text-sm font-medium">
        <span className={`size-2 rounded-full ${status === "live" ? "bg-ok-fg" : "bg-fg-muted"}`} aria-hidden />
        Зараз в ефірі <span className="font-normal text-fg-muted">· {STATUS[status]}</span>
      </h2>
      {calls.length === 0 ? (
        <p className="text-sm text-fg-muted">Активних дзвінків немає.</p>
      ) : (
        <ul className="grid gap-1 text-sm">
          {calls.map((call) => (
            <li key={call.callId} className="flex flex-wrap items-center gap-x-3">
              <Link to={call.callId} className="font-mono text-xs text-accent hover:underline">
                {call.callId.slice(0, 8)}
              </Link>
              <span>{botName(call.botId)}</span>
              <span className="text-fg-muted">з {formatTime(call.startedAt)}</span>
              <span className="text-fg-muted">реплік: {call.turns}</span>
              <span className="text-fg-muted">остання подія: {call.lastType}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
