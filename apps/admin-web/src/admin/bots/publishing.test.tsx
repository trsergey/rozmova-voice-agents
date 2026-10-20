// Publishing from the bot page: the gate's reasons, eval runs, publish, versions, rollback
// and the diff page, against a fake admin API.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";
import { publishBlocker } from "./Publishing";
import type { BotDetail, FlowDiff } from "./queries";

const EVAL = { passed: true, cases: 14, cases_passed: 14, finished_at: "2026-10-20T20:52:58Z", current: true };
const BOT: BotDetail = {
  id: "b-1",
  name: "Hvylia demo",
  provider: "openai",
  model: "gpt-4.1-mini",
  live_version_id: "v-3",
  live_version: 3,
  draft_updated_at: "2026-10-20T20:00:00Z",
  created_at: "2026-10-15T08:00:00Z",
  last_eval: EVAL,
};
const VERSIONS = [
  {
    id: "v-3",
    version: 3,
    note: "rollback of v1",
    created_at: "2026-10-20T19:00:00Z",
    created_by: "u-1",
    created_by_email: "admin@example.com",
    live: true,
    eval: { passed: true, cases: 14, cases_passed: 14, finished_at: "2026-10-20T18:00:00Z" },
  },
  {
    id: "v-2",
    version: 2,
    note: "promo step",
    created_at: "2026-10-20T18:30:00Z",
    created_by: "u-1",
    created_by_email: "admin@example.com",
    live: false,
    eval: { passed: false, cases: 14, cases_passed: 12, finished_at: "2026-10-20T18:20:00Z" },
  },
  {
    id: "v-1",
    version: 1,
    note: "seed: flows/hvylia-demo.json",
    created_at: "2026-10-15T08:00:00Z",
    created_by: null,
    created_by_email: null,
    live: false,
    eval: null,
  },
];
const CHANGED: FlowDiff = {
  nodes: [
    {
      id: "start",
      type: "start",
      status: "changed",
      changes: [{ path: "data.greeting", before: "Вітаю!", after: "Привіт!" }],
    },
    { id: "promo", type: "llm_prompt", status: "added", changes: [] },
  ],
  edges: [
    { source: "assist", route: "operator", status: "changed", target_before: "operator", target_after: "promo" },
  ],
};
const EMPTY: FlowDiff = { nodes: [], edges: [] };

const ok = (body: unknown): Route => () => ({ status: 200, body });
const VALID = { ok: true, errors: [], warnings: [] };

const api = (session = ADMIN, extra: Record<string, Route> = {}) => ({
  ...as(session),
  "GET /bots/b-1": ok(BOT),
  "GET /bots/b-1/credentials": ok([]),
  "GET /bots/b-1/versions": ok(VERSIONS),
  "POST /bots/b-1/validate": ok(VALID),
  "GET /bots/b-1/versions/3/diff/draft": ok({ before: 3, after: null, diff: CHANGED }),
  ...extra,
});

afterEach(() => vi.unstubAllGlobals());

describe("publishBlocker", () => {
  const invalid = { ok: false, errors: [{ node_id: "a", code: "no_start", message: "x" }], warnings: [] };

  test.each([
    ["validation errors first", BOT, invalid, CHANGED, "Чернетка має помилки (1): виправте їх у редакторі."],
    ["nothing new", BOT, VALID, EMPTY, "Чернетка не відрізняється від версії в ефірі."],
    ["no eval", { ...BOT, last_eval: null }, VALID, CHANGED, "Спершу запустіть eval чернетки."],
    ["stale eval", { ...BOT, last_eval: { ...EVAL, current: false } }, VALID, CHANGED, "Чернетка змінилась після eval: запустіть його знову."],
    ["failed eval", { ...BOT, last_eval: { ...EVAL, passed: false, cases_passed: 9 } }, VALID, CHANGED, "Eval не пройдено: 9 з 14 кейсів."],
    ["ready", BOT, VALID, CHANGED, null],
    ["first publish has no diff", { ...BOT, live_version: null, live_version_id: null }, VALID, undefined, null],
  ] as const)("%s", (_, bot, validation, diff, reason) => {
    expect(publishBlocker(bot as BotDetail, validation as never, diff)).toBe(reason);
  });
});

test("versions show notes, eval badges and the live one", async () => {
  fakeApi(api());
  open("/admin/bots/b-1");

  const live = await screen.findByRole("row", { name: /v3/ });
  expect(within(live).getByText("в ефірі")).toBeInTheDocument();
  expect(within(live).getByText("eval 14/14")).toBeInTheDocument();
  expect(within(live).queryByRole("button", { name: "Відкотити" })).not.toBeInTheDocument();
  expect(within(live).getByRole("link", { name: "Зміни" })).toHaveAttribute("href", "/admin/bots/b-1/versions/2/diff/3");
  const failed = screen.getByRole("row", { name: /promo step/ });
  expect(within(failed).getByText("eval 12/14")).toBeInTheDocument();
  const seeded = screen.getByRole("row", { name: /seed: flows/ });
  expect(within(seeded).getByText("без eval")).toBeInTheDocument();
  expect(within(seeded).getByText("seed")).toBeInTheDocument();
});

test("an admin rolls back after confirming", async () => {
  const calls = fakeApi(api(ADMIN, { "POST /bots/b-1/rollback/2": ok({ ...VERSIONS[0], id: "v-4", version: 4 }) }));
  open("/admin/bots/b-1");

  const user = userEvent.setup();
  const row = await screen.findByRole("row", { name: /promo step/ });
  await user.click(within(row).getByRole("button", { name: "Відкотити" }));
  expect(calls.some((call) => call.key.startsWith("POST /bots/b-1/rollback"))).toBe(false);
  await user.click(within(row).getByRole("button", { name: "Так, відкотити до v2" }));

  expect(await screen.findByText("Відкочено: в ефірі v4")).toBeInTheDocument();
  const rollback = calls.find((call) => call.key === "POST /bots/b-1/rollback/2");
  expect(rollback?.csrf).toBe("csrf-admin");
});

test("the publish form shows what will change and publishes with a note", async () => {
  const calls = fakeApi(api(ADMIN, { "POST /bots/b-1/publish": ok({ ...VERSIONS[0], id: "v-4", version: 4 }) }));
  open("/admin/bots/b-1");

  expect(await screen.findByText("Що зміниться порівняно з v3")).toBeInTheDocument();
  expect(screen.getByText("Привіт!")).toBeInTheDocument();
  const user = userEvent.setup();
  const publish = await screen.findByRole("button", { name: "Опублікувати" });
  await vi.waitFor(() => expect(publish).toBeEnabled());
  await user.type(screen.getByLabelText("Нотатка до версії"), "new greeting");
  await user.click(publish);

  expect(await screen.findByText("Опубліковано v4")).toBeInTheDocument();
  expect(calls.find((call) => call.key === "POST /bots/b-1/publish")?.body).toEqual({ note: "new greeting" });
});

test("publish is disabled with the reason", async () => {
  fakeApi(api(ADMIN, { "GET /bots/b-1": ok({ ...BOT, last_eval: { ...EVAL, current: false } }) }));
  open("/admin/bots/b-1");

  expect(await screen.findByText("Чернетка змінилась після eval: запустіть його знову.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Опублікувати" })).toBeDisabled();
});

test("the server's gate refusal is explained", async () => {
  fakeApi(
    api(ADMIN, {
      "POST /bots/b-1/publish": () => ({
        status: 409,
        body: { error: { code: "eval_required", message: "x", details: { reason: "stale" } } },
      }),
    }),
  );
  open("/admin/bots/b-1");

  const user = userEvent.setup();
  const publish = await screen.findByRole("button", { name: "Опублікувати" });
  await vi.waitFor(() => expect(publish).toBeEnabled());
  await user.click(publish);

  expect(await screen.findByRole("alert")).toHaveTextContent("Чернетка змінилась після eval: запустіть його знову.");
});

test("an eval run shows its progress and then the result", async () => {
  let polls = 0;
  const running = { id: "j-1", bot_id: "b-1", state: "running", draft_hash: "h", cases_total: 14, cases_done: 5, started_at: "2026-10-20T21:00:00Z" };
  const calls = fakeApi(
    api(ADMIN, {
      "POST /bots/b-1/evals": () => ({ status: 202, body: { job_id: "j-1" } }),
      "GET /evals/j-1": () => {
        polls += 1;
        return { status: 200, body: polls === 1 ? running : { ...running, state: "done", cases_done: 14, passed: true } };
      },
    }),
  );
  open("/admin/bots/b-1");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Запустити eval" }));

  expect(await screen.findByText("Eval триває: 5 з 14 кейсів…")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Запустити eval" })).toBeDisabled();
  expect(await screen.findByText(/Eval завершено/, {}, { timeout: 3000 })).toHaveTextContent("пройдено");
  // The bot card is refetched for the new report.
  expect(calls.filter((call) => call.key === "GET /bots/b-1").length).toBeGreaterThan(1);
});

test("viewers see versions but no publishing controls", async () => {
  fakeApi(api(VIEWER));
  open("/admin/bots/b-1");

  await screen.findByRole("row", { name: /promo step/ });
  expect(screen.queryByRole("button", { name: "Опублікувати" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Запустити eval" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Відкотити" })).not.toBeInTheDocument();
});

test("the diff page lists changed nodes and edges", async () => {
  fakeApi({ ...as(VIEWER), "GET /bots/b-1/versions/2/diff/3": ok({ before: 2, after: 3, diff: CHANGED }) });
  open("/admin/bots/b-1/versions/2/diff/3");

  expect(await screen.findByRole("heading", { name: "Зміни v2 → v3" })).toBeInTheDocument();
  expect(await screen.findByText("data.greeting")).toBeInTheDocument();
  const text = screen.getByLabelText("зміни в тексті");
  expect(within(text).getByRole("deletion")).toHaveTextContent("Вітаю!");
  expect(within(text).getByRole("insertion")).toHaveTextContent("Привіт!");
  expect(screen.getByText("promo")).toBeInTheDocument();
  expect(screen.getByText("operator", { selector: "s" })).toBeInTheDocument();
});
