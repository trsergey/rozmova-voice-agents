// The error screen of every route, instead of React Router's developer screen. A chunk of
// an older deploy reloads the page once (staleChunks.ts); if that does not help, or the
// error is anything else, the visitor gets a short message and a way out.
import { useState } from "react";
import { useRouteError } from "react-router";

import { isStaleChunkError, reloadOnce } from "./staleChunks";

export function RouteError() {
  const error = useRouteError();
  const stale = isStaleChunkError(error);
  // Decided once per error screen; reloadOnce is guarded, so a repeat call does nothing.
  const [reloading] = useState(() => stale && reloadOnce());

  if (reloading) return <p className="p-8 text-sm text-fg-muted">Оновлюємо сторінку…</p>;
  return (
    <main role="alert" className="mx-auto max-w-md p-8">
      <h1 className="mb-2 text-lg font-semibold">Щось пішло не так</h1>
      <p className="mb-4 text-sm text-fg-muted">
        {stale
          ? "Адмінку оновили, поки сторінка була відкрита. Оновіть сторінку, щоб завантажити нову версію."
          : "Сторінка не відкрилася через помилку. Спробуйте оновити її."}
      </p>
      <div className="flex gap-3">
        {/* A plain button: this screen is in the entry, which must not load admin/ui. */}
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg hover:bg-accent/90"
        >
          Оновити сторінку
        </button>
        {window.location.pathname.startsWith("/admin") ? (
          <a href="/admin/" className="px-3 py-2 text-sm text-accent hover:underline">
            До адмінки
          </a>
        ) : (
          <a href="/demo/" className="px-3 py-2 text-sm text-accent hover:underline">
            До демо
          </a>
        )}
      </div>
    </main>
  );
}
