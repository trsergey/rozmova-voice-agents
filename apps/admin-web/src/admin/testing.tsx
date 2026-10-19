// Test helpers for admin pages: a fake admin API on the global fetch and the app's routes
// in a memory router.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { routes } from "../router";

export const ADMIN = { user: { id: "u-1", name: "admin@example.com", role: "admin" }, csrf_token: "csrf-admin" };
export const VIEWER = { user: { id: "c-1", name: "Acme", role: "viewer" }, csrf_token: "csrf-viewer" };

type Reply = { status: number; body?: unknown };
export type Route = (body: unknown) => Reply;

export function fakeApi(routes: Record<string, Route>) {
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

export const anonymous = { "GET /auth/me": () => ({ status: 401, body: { error: { code: "not_authenticated", message: "x" } } }) };
export const as = (session: typeof ADMIN) => ({ "GET /auth/me": () => ({ status: 200, body: session }) });

export function open(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}
