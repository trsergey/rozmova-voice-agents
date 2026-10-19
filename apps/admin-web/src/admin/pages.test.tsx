// The knowledge base, ops and access code pages against a fake admin API.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "./testing";

const ok = (body: unknown): Route => () => ({ status: 200, body });
afterEach(() => vi.unstubAllGlobals());

// --- knowledge base ------------------------------------------------------------------------

const DOCS = [
  { id: "d-1", title: "Роумінг", source_path: "roaming.md", chunks: 4, updated_at: "2026-10-19T09:00:00Z" },
  { id: "d-2", title: "Тариф Безлім", source_path: "tariffs/bezlim.md", chunks: 7, updated_at: "2026-10-19T09:00:00Z" },
];
const IDLE = { state: "idle", started_at: null, finished_at: null, report: null };
const REPORT = {
  documents_added: 1,
  documents_updated: 0,
  documents_unchanged: 1,
  documents_deleted: 0,
  chunks_embedded: 3,
  chunks_reused: 8,
  chunks_deleted: 0,
  skipped_locked: false,
};

test("the KB page lists documents with chunk counts", async () => {
  fakeApi({ ...as(VIEWER), "GET /kb/documents": ok(DOCS), "GET /kb/reindex": ok(IDLE) });
  open("/admin/kb");

  const row = await screen.findByRole("row", { name: /Тариф Безлім/ });
  expect(within(row).getByText("tariffs/bezlim.md")).toBeInTheDocument();
  expect(within(row).getByText("7")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Переіндексувати" })).not.toBeInTheDocument();
});

test("reindex shows progress while it runs and the report after", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const started = "2026-10-19T10:00:00Z";
  let status: Record<string, unknown> = IDLE;
  let documents = DOCS.slice(0, 1);
  const calls = fakeApi({
    ...as(ADMIN),
    "GET /kb/documents": () => ({ status: 200, body: documents }),
    "GET /kb/reindex": () => ({ status: 200, body: status }),
    "POST /kb/reindex": () => {
      status = { state: "running", started_at: started, finished_at: null, report: null };
      return { status: 202, body: status };
    },
  });
  open("/admin/kb");

  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  await user.click(await screen.findByRole("button", { name: "Переіндексувати" }));

  expect(await screen.findByRole("progressbar")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Індексується…" })).toBeDisabled();
  expect(calls.find((call) => call.key === "POST /kb/reindex")?.csrf).toBe("csrf-admin");

  status = { state: "done", started_at: started, finished_at: "2026-10-19T10:00:20Z", report: REPORT };
  documents = DOCS;
  await vi.advanceTimersByTimeAsync(2100);

  expect(await screen.findByText(/нових ембедингів 3, перевикористано 8/)).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(await screen.findByRole("row", { name: /Тариф Безлім/ })).toBeInTheDocument();
  vi.useRealTimers();
});

test("reindex without an OpenAI key is explained", async () => {
  fakeApi({
    ...as(ADMIN),
    "GET /kb/documents": ok([]),
    "GET /kb/reindex": ok(IDLE),
    "POST /kb/reindex": () => ({ status: 503, body: { error: { code: "embeddings_unavailable", message: "x" } } }),
  });
  open("/admin/kb");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Переіндексувати" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Немає ключа OpenAI");
});

// --- ops -----------------------------------------------------------------------------------

const stats = (name: "1h" | "24h", turns: number) => ({
  window: name,
  calls: 3,
  turns,
  p95_ms: { turn: 4900, ttft: 3800, llm_ttft: 2300, rag_search: 1500, tool: null },
  tool_calls: 4,
  tool_errors: 1,
  tool_error_rate: 0.25,
  errors: 0,
  error_rate: 0,
});

test("the ops page shows active calls, p95 per stage and error rates", async () => {
  fakeApi({
    ...as(VIEWER),
    "GET /ops/summary": ok({
      generated_at: "2026-10-19T10:00:00Z",
      active_calls: 2,
      windows: [stats("1h", 9), stats("24h", 40)],
    }),
  });
  open("/admin/ops");

  expect(await screen.findByLabelText("Активних дзвінків")).toHaveTextContent("2");
  const hour = screen.getByRole("region", { name: "Остання година" });
  expect(hour).toHaveTextContent("Ходів9");
  expect(hour).toHaveTextContent("Хід повністю4.9 с");
  expect(hour).toHaveTextContent("Пошук у базі знань1.5 с");
  expect(hour).toHaveTextContent("Дія (виклик інструмента)—");
  expect(hour).toHaveTextContent("Помилки дій: 25.0% (1 з 4)");
  expect(screen.getByRole("region", { name: "Остання доба" })).toHaveTextContent("Ходів40");
});

test("unknown active calls are not shown as zero", async () => {
  fakeApi({
    ...as(ADMIN),
    "GET /ops/summary": ok({ generated_at: "2026-10-19T10:00:00Z", active_calls: null, windows: [] }),
  });
  open("/admin/ops");

  expect(await screen.findByLabelText("Активних дзвінків")).toHaveTextContent("—");
  expect(screen.getByText("Redis не відповідає")).toBeInTheDocument();
});

// --- access codes --------------------------------------------------------------------------

const CODE = {
  id: "c-1",
  label: "Acme",
  created_at: "2026-10-18T09:00:00Z",
  revoked_at: null,
  last_used_at: "2026-10-19T08:00:00Z",
};

test("an admin issues a code and sees it once", async () => {
  let codes: unknown[] = [CODE];
  const calls = fakeApi({
    ...as(ADMIN),
    "GET /access-codes": () => ({ status: 200, body: codes }),
    "POST /access-codes": (body) => {
      const issued = { ...CODE, id: "c-2", label: (body as { label: string }).label, last_used_at: null, code: "ABCD-EFGH-JKMN" };
      codes = [issued, CODE];
      return { status: 201, body: issued };
    },
  });
  open("/admin/access-codes");

  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Для кого (компанія або людина)"), "  Globex ");
  await user.click(screen.getByRole("button", { name: "Видати код" }));

  const issued = await screen.findByRole("status");
  expect(issued).toHaveTextContent("ABCD-EFGH-JKMN");
  expect(issued).toHaveTextContent("більше його не буде видно");
  expect(await screen.findByRole("row", { name: /Globex/ })).toBeInTheDocument();
  expect(calls.find((call) => call.key === "POST /access-codes")?.body).toEqual({ label: "Globex" });

  await user.click(screen.getByRole("button", { name: "Готово" }));
  expect(screen.queryByText("ABCD-EFGH-JKMN")).not.toBeInTheDocument();
});

test("revoking asks first, then marks the code revoked", async () => {
  let codes: unknown[] = [CODE];
  const calls = fakeApi({
    ...as(ADMIN),
    "GET /access-codes": () => ({ status: 200, body: codes }),
    "DELETE /access-codes/c-1": () => {
      codes = [{ ...CODE, revoked_at: "2026-10-19T10:00:00Z" }];
      return { status: 204 };
    },
  });
  open("/admin/access-codes");

  const user = userEvent.setup();
  const row = await screen.findByRole("row", { name: /Acme/ });
  expect(within(row).getByText("діє")).toBeInTheDocument();
  await user.click(within(row).getByRole("button", { name: "Відкликати" }));
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent("сесії з цим кодом завершаться");
  await user.click(within(dialog).getByRole("button", { name: "Відкликати" }));

  expect(await within(row).findByText("відкликаний")).toBeInTheDocument();
  expect(within(row).queryByRole("button", { name: "Відкликати" })).not.toBeInTheDocument();
  expect(calls.find((call) => call.key === "DELETE /access-codes/c-1")?.csrf).toBe("csrf-admin");
});
