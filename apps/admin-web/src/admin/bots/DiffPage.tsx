// What changed between two versions of a bot, or between a version and the draft (FR-12):
// nodes and edges, without canvas positions.
import { Link, useParams } from "react-router";

import { ApiError } from "../../api/client";
import { ErrorText, PageHeader } from "../ui";
import { DiffView } from "./Publishing";
import { useDiff, type DiffSide } from "./queries";

export function DiffPage() {
  const { botId = "", a = "", b = "" } = useParams();
  const before = Number(a);
  const after: DiffSide = b === "draft" ? "draft" : Number(b);
  const diff = useDiff(botId, Number.isInteger(before) ? before : null, after);

  return (
    <>
      <Link to={`/admin/bots/${botId}`} className="mb-3 inline-block text-sm text-accent hover:underline">
        ← До бота
      </Link>
      <PageHeader title={`Зміни v${a} → ${after === "draft" ? "чернетка" : `v${b}`}`} />
      {diff.isPending && <p className="text-sm text-fg-muted">Завантаження…</p>}
      {diff.isError && (
        <ErrorText>
          {diff.error instanceof ApiError && diff.error.status === 404 ? "Такої версії немає." : "Не вдалося порівняти версії."}
        </ErrorText>
      )}
      {diff.data && (
        <div className="max-w-3xl rounded-lg border border-line bg-surface p-4">
          <DiffView diff={diff.data.diff} />
        </div>
      )}
    </>
  );
}
