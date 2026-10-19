// The admin shell against a fake admin API: the auth guard, both logins, the viewer's
// read-only mode and logout.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN, anonymous, as, fakeApi, open, VIEWER } from "./testing";

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
