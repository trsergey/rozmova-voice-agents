// Publishing a bot from its page (FR-7, FR-12, FR-19): what the next publish would change,
// the eval of the draft with its progress, the publish button with the reason it is
// disabled, and the version history with an eval badge, a diff and a rollback per version.
// admin-api enforces the same gate; the page explains it before the click.
import { useState } from "react";
import { Link } from "react-router";

import { ApiError, type Schemas } from "../../api/client";
import { NODE_KINDS, type NodeType } from "../editor/graph";
import { formatDateTime } from "../format";
import { AdminOnly } from "../guards";
import { Badge, Button, Card, ErrorText, Field, Input } from "../ui";
import {
  useDiff,
  useEvalJob,
  usePublish,
  useRollback,
  useStartEval,
  useValidation,
  useVersions,
  type BotDetail,
  type FlowDiff,
  type Version,
} from "./queries";

type Validation = Schemas["ValidationResult"];

// Why the draft cannot be published now, or null. The server checks the same in this order.
export function publishBlocker(bot: BotDetail, validation: Validation | undefined, draftDiff: FlowDiff | undefined) {
  const errors = validation?.errors?.length ?? 0;
  if (errors > 0) return `Чернетка має помилки (${errors}): виправте їх у редакторі.`;
  if (bot.live_version != null && draftDiff && isEmpty(draftDiff)) {
    return "Чернетка не відрізняється від версії в ефірі.";
  }
  const run = bot.last_eval;
  if (!run) return "Спершу запустіть eval чернетки.";
  if (!run.current) return "Чернетка змінилась після eval: запустіть його знову.";
  if (!run.passed) return `Eval не пройдено: ${run.cases_passed} з ${run.cases} кейсів.`;
  return null;
}

const GATE_REASONS: Record<string, string> = {
  missing: "Спершу запустіть eval чернетки.",
  stale: "Чернетка змінилась після eval: запустіть його знову.",
  failed: "Останній eval чернетки не пройдено.",
};

function errorText(error: Error | null): string {
  if (!(error instanceof ApiError)) return "Не вдалося виконати дію.";
  if (error.code === "eval_required") return GATE_REASONS[String(error.details.reason)] ?? error.message;
  if (error.code === "draft_invalid") return "Чернетка має помилки: виправте їх у редакторі.";
  if (error.code === "nothing_to_publish") return "Ця логіка вже в ефірі.";
  if (error.code === "eval_unavailable") return "Eval зараз недоступний: на сервері немає ключа LLM чи бази знань.";
  return error.message;
}

function isEmpty(diff: FlowDiff) {
  return diff.nodes.length === 0 && diff.edges.length === 0;
}

export function PublishCard({ bot }: { bot: BotDetail }) {
  const validation = useValidation(bot.id, bot.draft_updated_at);
  const draftDiff = useDiff(bot.id, bot.live_version ?? null, "draft");
  const changes = draftDiff.data?.diff;
  const blocker = validation.isPending || draftDiff.isLoading ? null : publishBlocker(bot, validation.data, changes);

  return (
    <Card title="Публікація чернетки">
      <div className="grid gap-4">
        <EvalRun bot={bot} />
        <PublishForm botId={bot.id} blocker={blocker} checking={validation.isPending || draftDiff.isLoading} />
        {bot.live_version != null && changes && (
          <details className="text-sm" open={!isEmpty(changes)}>
            <summary className="cursor-pointer font-medium">Що зміниться порівняно з v{bot.live_version}</summary>
            <div className="mt-3">
              <DiffView diff={changes} />
            </div>
          </details>
        )}
      </div>
    </Card>
  );
}

function EvalRun({ bot }: { bot: BotDetail }) {
  const start = useStartEval(bot.id);
  const job = useEvalJob(bot.id, start.data?.job_id ?? null);
  const running = start.isPending || job.data?.state === "running";

  return (
    <div className="grid gap-2 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <AdminOnly>
          <Button variant="secondary" onClick={() => start.mutate()} disabled={running}>
            Запустити eval
          </Button>
        </AdminOnly>
        {job.data?.state === "running" && (
          <span role="status">
            Eval триває: {job.data.cases_done} з {job.data.cases_total} кейсів…
          </span>
        )}
        {job.data?.state === "done" && (
          <span role="status">
            Eval завершено:{" "}
            <Badge tone={job.data.passed ? "ok" : "bad"}>{job.data.passed ? "пройдено" : "не пройдено"}</Badge>
          </span>
        )}
      </div>
      {job.data?.state === "failed" && (
        <ErrorText>{job.data.error === "timeout" ? "Eval перевищив ліміт часу." : "Eval не вдалося завершити."}</ErrorText>
      )}
      {start.isError && <ErrorText>{errorText(start.error)}</ErrorText>}
      <p className="text-xs text-fg-muted">
        Швидкий набір еталонних діалогів без LLM-судді, на ключі бота; результат видно в «Останній eval».
      </p>
    </div>
  );
}

function PublishForm({ botId, blocker, checking }: { botId: string; blocker: string | null; checking: boolean }) {
  const publish = usePublish(botId);
  const [note, setNote] = useState("");

  return (
    <AdminOnly>
      <form
        className="grid gap-2 border-t border-line pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          publish.mutate(note, { onSuccess: () => setNote("") });
        }}
      >
        <Field label="Нотатка до версії" htmlFor="publish-note">
          <Input
            id="publish-note"
            value={note}
            maxLength={500}
            placeholder="Що змінилось"
            onChange={(e) => setNote(e.target.value)}
          />
        </Field>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={checking || blocker != null || publish.isPending}>
            Опублікувати
          </Button>
          {blocker && <span className="text-sm text-warn-fg">{blocker}</span>}
          {publish.isSuccess && (
            <span role="status" className="text-sm text-ok-fg">
              Опубліковано v{publish.data.version}
            </span>
          )}
        </div>
        {publish.isError && <ErrorText>{errorText(publish.error)}</ErrorText>}
      </form>
    </AdminOnly>
  );
}

export function VersionsCard({ botId }: { botId: string }) {
  const versions = useVersions(botId);
  const rollback = useRollback(botId);
  const [confirming, setConfirming] = useState<number | null>(null);

  return (
    <Card title="Версії">
      {versions.isError && <ErrorText>Не вдалося завантажити версії.</ErrorText>}
      {versions.data?.length === 0 && <p className="text-sm text-fg-muted">Ще жодної версії.</p>}
      {versions.data && versions.data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-fg-muted">
              <tr>
                <th className="py-2 pr-3 font-medium">Версія</th>
                <th className="py-2 pr-3 font-medium">Нотатка</th>
                <th className="py-2 pr-3 font-medium">Eval</th>
                <th className="py-2 pr-3 font-medium">Хто й коли</th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {versions.data.map((version) => (
                <tr key={version.id} className="border-t border-line align-top">
                  <td className="py-2 pr-3 whitespace-nowrap">
                    v{version.version} {version.live && <Badge tone="ok">в ефірі</Badge>}
                  </td>
                  <td className="py-2 pr-3">{version.note ?? <span className="text-fg-muted">—</span>}</td>
                  <td className="py-2 pr-3 whitespace-nowrap">
                    <EvalBadge version={version} />
                  </td>
                  <td className="py-2 pr-3 text-fg-muted">
                    <span>{version.created_by_email ?? "seed"}</span>
                    <br />
                    {formatDateTime(version.created_at)}
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap justify-end gap-2">
                      {version.version > 1 && (
                        <Link
                          to={`versions/${version.version - 1}/diff/${version.version}`}
                          className="rounded-md px-2 py-1 text-accent hover:underline"
                        >
                          Зміни
                        </Link>
                      )}
                      {!version.live && (
                        <AdminOnly>
                          {confirming === version.version ? (
                            <Button
                              onClick={() =>
                                rollback.mutate(version.version, { onSettled: () => setConfirming(null) })
                              }
                              disabled={rollback.isPending}
                            >
                              Так, відкотити до v{version.version}
                            </Button>
                          ) : (
                            <Button variant="secondary" onClick={() => setConfirming(version.version)}>
                              Відкотити
                            </Button>
                          )}
                        </AdminOnly>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rollback.isError && <ErrorText>{errorText(rollback.error)}</ErrorText>}
      {rollback.isSuccess && (
        <p role="status" className="mt-2 text-sm text-ok-fg">
          Відкочено: в ефірі v{rollback.data.version}
        </p>
      )}
    </Card>
  );
}

function EvalBadge({ version }: { version: Version }) {
  const run = version.eval;
  if (!run) return <Badge tone="neutral">без eval</Badge>;
  return (
    <Badge tone={run.passed ? "ok" : "bad"}>
      eval {run.cases_passed}/{run.cases}
    </Badge>
  );
}

const STATUS: Record<FlowDiff["nodes"][number]["status"], { label: string; tone: "ok" | "bad" | "neutral" }> = {
  added: { label: "додано", tone: "ok" },
  removed: { label: "видалено", tone: "bad" },
  changed: { label: "змінено", tone: "neutral" },
};

export function DiffView({ diff }: { diff: FlowDiff }) {
  if (isEmpty(diff)) return <p className="text-sm text-fg-muted">Логіка не змінилась.</p>;
  const { nodes, edges } = diff;
  return (
    <div className="grid gap-4 text-sm">
      {nodes.length > 0 && (
        <section>
          <h3 className="mb-2 font-medium">Кроки</h3>
          <ul className="grid gap-2">
            {nodes.map((node) => (
              <li key={node.id} className="rounded-md border border-line p-2">
                <p className="flex flex-wrap items-center gap-2">
                  <Badge tone={STATUS[node.status].tone}>{STATUS[node.status].label}</Badge>
                  <span className="font-mono">{node.id}</span>
                  <span className="text-fg-muted">{NODE_KINDS[node.type as NodeType]?.label ?? node.type}</span>
                </p>
                {node.changes.length > 0 && (
                  <dl className="mt-2 grid gap-1.5">
                    {node.changes.map((change) => (
                      <div key={change.path} className="grid gap-1 sm:grid-cols-[10rem_1fr]">
                        <dt className="font-mono text-fg-muted">{change.path}</dt>
                        <dd className="grid gap-1">
                          <Value tone="before" value={change.before} />
                          <Value tone="after" value={change.after} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {edges.length > 0 && (
        <section>
          <h3 className="mb-2 font-medium">Зв'язки</h3>
          <ul className="grid gap-1.5">
            {edges.map((edge) => (
              <li
                key={`${edge.source}:${edge.route ?? ""}:${edge.target_before ?? ""}:${edge.target_after ?? ""}`}
                className="flex flex-wrap items-center gap-2"
              >
                <Badge tone={STATUS[edge.status].tone}>{STATUS[edge.status].label}</Badge>
                <span className="font-mono">
                  {edge.source}
                  {edge.route ? ` [${edge.route}]` : ""} →{" "}
                  {edge.status === "changed" ? (
                    <>
                      <s className="text-fg-muted">{edge.target_before}</s> {edge.target_after}
                    </>
                  ) : (
                    (edge.target_after ?? edge.target_before)
                  )}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Value({ tone, value }: { tone: "before" | "after"; value: unknown }) {
  const text = value === null || value === undefined ? "—" : typeof value === "string" ? value : JSON.stringify(value);
  return (
    <pre
      className={`rounded px-2 py-1 font-mono text-xs whitespace-pre-wrap break-words ${
        tone === "before" ? "bg-danger/10 text-danger" : "bg-ok-bg text-ok-fg"
      }`}
      aria-label={tone === "before" ? "було" : "стало"}
    >
      {text}
    </pre>
  );
}
