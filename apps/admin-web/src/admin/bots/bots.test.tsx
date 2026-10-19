// The bot list and bot page against a fake admin API.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN, as, fakeApi, open, VIEWER, type Route } from "../testing";

const LIVE = {
  id: "b-1",
  name: "Hvylia demo",
  provider: "openai",
  model: "gpt-4.1-mini",
  live_version_id: "v-3",
  live_version: 3,
  draft_updated_at: "2026-10-19T09:30:00Z",
  created_at: "2026-10-15T08:00:00Z",
};
const DRAFT_ONLY = { ...LIVE, id: "b-2", name: "Roaming bot", live_version_id: null, live_version: null, draft_updated_at: null };
const EVAL = { passed: true, cases: 50, cases_passed: 50, finished_at: "2026-10-19T10:00:00Z", current: true };

const ok = (body: unknown): Route => () => ({ status: 200, body });

const api = (session = ADMIN, extra: Record<string, Route> = {}) => ({
  ...as(session),
  "GET /bots": ok([LIVE, DRAFT_ONLY]),
  "GET /providers": ok(["anthropic", "openai"]),
  "GET /bots/b-1": ok({ ...LIVE, last_eval: EVAL }),
  "GET /bots/b-1/credentials": ok([]),
  ...extra,
});

afterEach(() => vi.unstubAllGlobals());

test("the list shows what is live and links each bot", async () => {
  fakeApi(api());
  open("/admin/bots");

  const live = await screen.findByRole("row", { name: /Hvylia demo/ });
  expect(within(live).getByText("v3")).toBeInTheDocument();
  expect(within(live).getByText("openai · gpt-4.1-mini")).toBeInTheDocument();
  const draft = screen.getByRole("row", { name: /Roaming bot/ });
  expect(within(draft).getByText("не опубліковано")).toBeInTheDocument();
  expect(within(draft).getByRole("link", { name: "Roaming bot" })).toHaveAttribute("href", "/admin/bots/b-2");
});

test("an admin creates a bot and lands on its page", async () => {
  const created = { ...DRAFT_ONLY, id: "b-9", name: "New bot", provider: "anthropic", model: "claude-haiku" };
  const calls = fakeApi(
    api(ADMIN, {
      "POST /bots": ok(created),
      "GET /bots/b-9": ok({ ...created, last_eval: null }),
      "GET /bots/b-9/credentials": ok([]),
    }),
  );
  const router = open("/admin/bots");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Новий бот" }));
  const dialog = await screen.findByRole("dialog");
  await user.type(within(dialog).getByLabelText("Назва"), "New bot");
  await within(dialog).findByRole("option", { name: "anthropic" });
  await user.selectOptions(within(dialog).getByLabelText("Провайдер"), "anthropic");
  await user.clear(within(dialog).getByLabelText("Модель"));
  await user.type(within(dialog).getByLabelText("Модель"), "claude-haiku");
  await user.click(within(dialog).getByRole("button", { name: "Створити" }));

  expect(await screen.findByRole("heading", { name: "New bot" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/admin/bots/b-9");
  const post = calls.find((call) => call.key === "POST /bots");
  expect(post?.body).toEqual({ name: "New bot", provider: "anthropic", model: "claude-haiku" });
  expect(post?.csrf).toBe("csrf-admin");
});

test("viewers cannot create bots", async () => {
  fakeApi(api(VIEWER));
  open("/admin/bots");

  await screen.findByRole("row", { name: /Hvylia demo/ });
  expect(screen.queryByRole("button", { name: "Новий бот" })).not.toBeInTheDocument();
});

test("the bot page shows the live version, the draft and the last eval", async () => {
  fakeApi(api(VIEWER, { "GET /bots/b-1": ok({ ...LIVE, last_eval: { ...EVAL, current: false } }) }));
  open("/admin/bots/b-1");

  expect(await screen.findByRole("heading", { name: "Hvylia demo" })).toBeInTheDocument();
  expect(screen.getByText("v3")).toBeInTheDocument();
  expect(screen.getByText(/^Змінена /)).toBeInTheDocument();
  expect(screen.getByText("пройдено")).toBeInTheDocument();
  expect(screen.getByText("50 з 50 кейсів")).toBeInTheDocument();
  expect(screen.getByText("Чернетка змінилась після прогону.")).toBeInTheDocument();
});

test("a bot without eval or publication says so", async () => {
  fakeApi(api(ADMIN, { "GET /bots/b-1": ok({ ...DRAFT_ONLY, id: "b-1", last_eval: null }) }));
  open("/admin/bots/b-1");

  expect(await screen.findByText("Ще не опубліковано")).toBeInTheDocument();
  expect(screen.getByText("Ще не змінювалась")).toBeInTheDocument();
  expect(screen.getByText("Ще не запускався")).toBeInTheDocument();
  expect(await screen.findByText(/працює на демо-ключі/)).toBeInTheDocument();
});

test("a saved key shows only its last 4 characters and is not kept in the form", async () => {
  const key = "sk-proj-very-secret-key-a1b2";
  let stored: unknown[] = [];
  const calls = fakeApi(
    api(ADMIN, {
      "GET /bots/b-1/credentials": () => ({ status: 200, body: stored }),
      "PUT /bots/b-1/credentials": (body) => {
        const saved = { provider: (body as { provider: string }).provider, last4: "a1b2", created_at: "2026-10-19T11:00:00Z" };
        stored = [saved];
        return { status: 200, body: saved };
      },
    }),
  );
  open("/admin/bots/b-1");

  const user = userEvent.setup();
  const input = await screen.findByLabelText("Новий ключ");
  expect(input).toHaveAttribute("type", "password");
  await user.type(input, key);
  await user.click(screen.getByRole("button", { name: "Зберегти ключ" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Ключ збережено: …a1b2");
  expect(await screen.findByText("…a1b2")).toBeInTheDocument();
  expect(input).toHaveValue("");
  expect(document.body.textContent).not.toContain(key);
  const put = calls.find((call) => call.key === "PUT /bots/b-1/credentials");
  expect(put?.body).toEqual({ provider: "openai", api_key: key });
  expect(put?.csrf).toBe("csrf-admin");
});

test("without a master key the server refusal is explained", async () => {
  fakeApi(
    api(ADMIN, {
      "PUT /bots/b-1/credentials": () => ({
        status: 503,
        body: { error: { code: "credentials_unavailable", message: "x" } },
      }),
    }),
  );
  open("/admin/bots/b-1");

  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Новий ключ"), "sk-proj-very-secret-key-a1b2");
  await user.click(screen.getByRole("button", { name: "Зберегти ключ" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("немає майстер-ключа");
});

test("viewers see the keys' last 4 characters but no form", async () => {
  fakeApi(
    api(VIEWER, {
      "GET /bots/b-1/credentials": ok([{ provider: "openai", last4: "zz99", created_at: "2026-10-19T11:00:00Z" }]),
    }),
  );
  open("/admin/bots/b-1");

  expect(await screen.findByText("…zz99")).toBeInTheDocument();
  expect(screen.queryByLabelText("Новий ключ")).not.toBeInTheDocument();
});

test("an unknown bot id shows not found", async () => {
  fakeApi(api());
  open("/admin/bots/missing");

  expect(await screen.findByRole("heading", { name: "Бота не знайдено" })).toBeInTheDocument();
});
