// The admin shell against a fake admin API: the auth guard, both logins, the viewer's
// read-only mode and logout.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "../router";

const ADMIN = { user: { id: "u-1", name: "admin@example.com", role: "admin" }, csrf_token: "csrf-admin" };
const VIEWER = { user: { id: "c-1", name: "Acme", role: "viewer" }, csrf_token: "csrf-viewer" };

type Reply = { status: number; body?: unknown };
type Route = (body: unknown) => Reply;

function fakeApi(routes: Record<string, Route>) {
  const calls: { key: string; body: unknown; csrf: string | null }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const key = `${request.method} ${new URL(request.url).pathname}`;
      const text = await request.text();
      const body = text ? JSON.parse(text) : undefined;
      calls.push({ key, body, csrf: request.headers.get("X-CSRF-Token") });
      const route = routes[key];
      const { status, body: out } = route ? route(body) : { status: 404, body: { error: { code: "not_found", message: "x" } } };
      return new Response(out === undefined ? null : JSON.stringify(out), { status });
    }),
  );
  return calls;
}

const anonymous = { "GET /auth/me": () => ({ status: 401, body: { error: { code: "not_authenticated", message: "x" } } }) };
const as = (session: typeof ADMIN) => ({ "GET /auth/me": () => ({ status: 200, body: session }) });

function open(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

afterEach(() => vi.unstubAllGlobals());

test("an anonymous visitor goes to the login page and comes back after logging in", async () => {
  const calls = fakeApi({ ...anonymous, "POST /auth/login": () => ({ status: 200, body: ADMIN }) });
  const router = open("/admin/calls");

  await screen.findByRole("tab", { name: "Адміністратор" });
  expect(router.state.location.pathname).toBe("/admin/login");

  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), "admin@example.com");
  await user.type(screen.getByLabelText("Пароль"), "secret-password");
  await user.click(screen.getByRole("button", { name: "Увійти" }));

  expect(await screen.findByRole("heading", { name: "Дзвінки" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/admin/calls");
  expect(calls.find((call) => call.key === "POST /auth/login")?.body).toEqual({
    email: "admin@example.com",
    password: "secret-password",
  });
});

test("wrong credentials show an error and stay on the login page", async () => {
  fakeApi({
    ...anonymous,
    "POST /auth/login": () => ({ status: 401, body: { error: { code: "invalid_credentials", message: "x" } } }),
  });
  open("/admin/login");

  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Email"), "admin@example.com");
  await user.type(screen.getByLabelText("Пароль"), "wrong");
  await user.click(screen.getByRole("button", { name: "Увійти" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Невірний email або пароль.");
});

test("an access code starts a read-only session", async () => {
  const calls = fakeApi({ ...anonymous, "POST /auth/access-code": () => ({ status: 200, body: VIEWER }) });
  open("/admin/login");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: "Код доступу" }));
  await user.type(screen.getByRole("textbox", { name: "Код доступу" }), "  ACME-1234 ");
  await user.click(screen.getByRole("button", { name: "Увійти" }));

  expect(await screen.findByRole("heading", { name: "Боти" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Режим перегляду");
  expect(calls.find((call) => call.key === "POST /auth/access-code")?.body).toEqual({ code: "ACME-1234" });
});

test("next never leads outside the admin", async () => {
  fakeApi(as(ADMIN));
  const router = open("/admin/login?next=https://evil.example/");

  await screen.findByRole("heading", { name: "Боти" });
  expect(router.state.location.pathname).toBe("/admin/bots");
});

test("viewers do not see admin-only pages", async () => {
  fakeApi(as(VIEWER));
  open("/admin/access-codes");

  expect(await screen.findByRole("heading", { name: "Немає доступу" })).toBeInTheDocument();
  const nav = screen.getByRole("navigation", { name: "Розділи" });
  expect(within(nav).queryByRole("link", { name: "Коди доступу" })).not.toBeInTheDocument();
});

test("admins see every section and no read-only banner", async () => {
  fakeApi(as(ADMIN));
  open("/admin");

  expect(await screen.findByRole("heading", { name: "Боти" })).toBeInTheDocument();
  const nav = screen.getByRole("navigation", { name: "Розділи" });
  expect(within(nav).getByRole("link", { name: "Коди доступу" })).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("logout sends the CSRF token and returns to the login page", async () => {
  const calls = fakeApi({ ...as(ADMIN), "POST /auth/logout": () => ({ status: 204 }) });
  const router = open("/admin/bots");

  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "admin@example.com, меню" }));
  await user.click(await screen.findByRole("menuitem", { name: "Вийти" }));

  await screen.findByRole("tab", { name: "Адміністратор" });
  expect(router.state.location.pathname).toBe("/admin/login");
  expect(calls.find((call) => call.key === "POST /auth/logout")?.csrf).toBe("csrf-admin");
});

test("an API outage is not mistaken for being logged out", async () => {
  fakeApi({ "GET /auth/me": () => ({ status: 503, body: { error: { code: "unavailable", message: "x" } } }) });
  open("/admin/bots");

  expect(await screen.findByText(/Адмін-API недоступний/)).toBeInTheDocument();
});

test("unknown admin paths show not found inside the shell", async () => {
  fakeApi(as(ADMIN));
  open("/admin/nope");

  expect(await screen.findByRole("heading", { name: "Сторінку не знайдено" })).toBeInTheDocument();
});
