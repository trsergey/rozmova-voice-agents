// A chunk of an older deploy reloads the page once; the guard stops a reload loop (ROZ-95).
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { RouteError } from "./RouteError";
import { isStaleChunkError, loadChunk, RELOAD_GUARD_MS, reloadOnce, resetReloadGuard } from "./staleChunks";

const STALE = new TypeError(
  "Failed to fetch dynamically imported module: https://x.example/assets/BotPage-CnoyTzwA.js",
);

beforeEach(() => {
  sessionStorage.clear();
  resetReloadGuard();
  vi.useRealTimers();
});

test("the three browsers' wordings count as a stale chunk", () => {
  expect(isStaleChunkError(STALE)).toBe(true);
  expect(isStaleChunkError(new TypeError("Importing a module script failed."))).toBe(true);
  expect(isStaleChunkError(new Error("error loading dynamically imported module: /a.js"))).toBe(true);
  expect(isStaleChunkError(new Error("Cannot read properties of undefined"))).toBe(false);
});

test("a stale chunk reloads once and the import never settles", async () => {
  const reload = vi.fn();
  let settled = false;
  void loadChunk(() => Promise.reject(STALE), reload).finally(() => (settled = true));
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(reload).toHaveBeenCalledTimes(1);
  expect(settled).toBe(false);
});

test("a second stale chunk right after a reload is thrown, not reloaded again", async () => {
  vi.useFakeTimers({ now: 1_000_000 });
  sessionStorage.setItem("chunk-reload-at", String(Date.now() - 1000));
  const reload = vi.fn();

  await expect(loadChunk(() => Promise.reject(STALE), reload)).rejects.toBe(STALE);
  expect(reload).not.toHaveBeenCalled();

  // Past the guard (and on a new page load) it reloads again.
  vi.setSystemTime(Date.now() + RELOAD_GUARD_MS);
  expect(reloadOnce(reload)).toBe(true);
});

test("other import errors are thrown as they are", async () => {
  const reload = vi.fn();
  const broken = new SyntaxError("Unexpected token");

  await expect(loadChunk(() => Promise.reject(broken), reload)).rejects.toBe(broken);
  expect(reload).not.toHaveBeenCalled();
});

function renderError(error: Error) {
  const router = createMemoryRouter(
    [
      {
        path: "/admin/bots",
        ErrorBoundary: RouteError,
        loader: () => {
          throw error;
        },
        Component: () => null,
      },
    ],
    { initialEntries: ["/admin/bots"] },
  );
  render(<RouterProvider router={router} />);
}

test("the error screen replaces React Router's developer screen", async () => {
  renderError(new Error("boom"));

  expect(await screen.findByRole("alert")).toHaveTextContent("Щось пішло не так");
  expect(screen.getByRole("button", { name: "Оновити сторінку" })).toBeInTheDocument();
  expect(screen.queryByText(/Unexpected Application Error/)).not.toBeInTheDocument();
});

test("a stale chunk that already reloaded explains the new version", async () => {
  sessionStorage.setItem("chunk-reload-at", String(Date.now()));
  renderError(STALE);

  expect(await screen.findByRole("alert")).toHaveTextContent("Адмінку оновили");
});
