// The knowledge base (FR-15): the indexed documents with their chunk counts, and the
// reindex, which runs in the background on the server; the page polls it while it runs.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { ApiError, client, csrf, unwrap, type Schemas } from "../../api/client";
import { formatDateTime } from "../format";
import { AdminOnly } from "../guards";
import { useCurrentSession } from "../session";
import { Badge, Button, Card, ErrorText, PageHeader } from "../ui";

type Status = Schemas["ReindexStatus"];

const POLL_MS = 2000;
const keys = { documents: ["kb", "documents"] as const, status: ["kb", "reindex"] as const };

export function KbPage() {
  const queryClient = useQueryClient();
  const documents = useQuery({ queryKey: keys.documents, queryFn: () => unwrap(client.GET("/kb/documents")) });
  const status = useQuery({
    queryKey: keys.status,
    queryFn: () => unwrap(client.GET("/kb/reindex")),
    refetchInterval: (query) => (query.state.data?.state === "running" ? POLL_MS : false),
  });

  // When a run finishes, the document list has changed.
  const state = status.data?.state;
  const previous = useRef(state);
  useEffect(() => {
    if (previous.current === "running" && state !== "running") {
      void queryClient.invalidateQueries({ queryKey: keys.documents });
    }
    previous.current = state;
  }, [state, queryClient]);

  return (
    <>
      <PageHeader title="База знань">
        <AdminOnly>
          <ReindexButton running={state === "running"} />
        </AdminOnly>
      </PageHeader>
      <div className="mb-6 max-w-xl">
        <Card title="Індексація">{status.data ? <ReindexStatus status={status.data} /> : null}</Card>
      </div>
      {documents.isError && <ErrorText>Не вдалося завантажити документи.</ErrorText>}
      {documents.data?.length === 0 && <p className="text-sm text-fg-muted">Документів у індексі ще немає.</p>}
      {documents.data && documents.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Документ</th>
                <th className="px-4 py-2 font-medium">Файл</th>
                <th className="px-4 py-2 text-right font-medium">Фрагментів</th>
                <th className="px-4 py-2 font-medium">Оновлено</th>
              </tr>
            </thead>
            <tbody>
              {documents.data.map((doc) => (
                <tr key={doc.id} className="border-b border-line last:border-b-0">
                  <td className="px-4 py-3">{doc.title}</td>
                  <td className="px-4 py-3 font-mono text-xs text-fg-muted">{doc.source_path}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{doc.chunks}</td>
                  <td className="px-4 py-3 text-fg-muted">{formatDateTime(doc.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function ReindexButton({ running }: { running: boolean }) {
  const queryClient = useQueryClient();
  const { csrf_token } = useCurrentSession();
  const reindex = useMutation({
    mutationFn: () => unwrap(client.POST("/kb/reindex", { headers: csrf(csrf_token) })),
    onSuccess: (status) => queryClient.setQueryData(keys.status, status),
  });
  const unavailable = reindex.error instanceof ApiError && reindex.error.code === "embeddings_unavailable";

  return (
    <div className="grid justify-items-end gap-1">
      <Button disabled={running || reindex.isPending} onClick={() => reindex.mutate()}>
        {running ? "Індексується…" : "Переіндексувати"}
      </Button>
      {reindex.isError && (
        <ErrorText>{unavailable ? "Немає ключа OpenAI для ембедингів." : "Не вдалося запустити."}</ErrorText>
      )}
    </div>
  );
}

function ReindexStatus({ status }: { status: Status }) {
  const report = status.report;
  return (
    <div className="grid gap-2 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <StateBadge state={status.state} />
        {status.started_at && <span className="text-fg-muted">з {formatDateTime(status.started_at)}</span>}
        {status.finished_at && <span className="text-fg-muted">до {formatDateTime(status.finished_at)}</span>}
      </p>
      {status.state === "running" && (
        <div role="progressbar" aria-label="Індексація триває" className="h-1.5 overflow-hidden rounded bg-muted-bg">
          <div className="h-1.5 w-1/3 animate-pulse rounded bg-accent" />
        </div>
      )}
      {report &&
        (report.skipped_locked ? (
          <p className="text-fg-muted">Пропущено: іншу індексацію вже було запущено.</p>
        ) : (
          <p className="text-fg-muted">
            Документи: +{report.documents_added}, змінено {report.documents_updated}, без змін{" "}
            {report.documents_unchanged}, видалено {report.documents_deleted}. Фрагменти: нових ембедингів{" "}
            {report.chunks_embedded}, перевикористано {report.chunks_reused}, видалено {report.chunks_deleted}.
          </p>
        ))}
      {status.state === "failed" && <p className="text-danger">Індексація впала; подробиці в логах admin-api.</p>}
    </div>
  );
}

function StateBadge({ state }: { state: Status["state"] }) {
  switch (state) {
    case "idle":
      return <Badge tone="neutral">не запускалась після старту сервера</Badge>;
    case "running":
      return <Badge tone="neutral">триває</Badge>;
    case "done":
      return <Badge tone="ok">готово</Badge>;
    case "failed":
      return <Badge tone="bad">помилка</Badge>;
  }
}
