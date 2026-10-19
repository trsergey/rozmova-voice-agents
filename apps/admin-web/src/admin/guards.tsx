// Route guards: RequireSession sends anonymous visitors to the login page and back after;
// RequireAdmin closes admin-only pages to viewers; AdminOnly hides admin-only controls.
import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";

import { SessionContext, useCanEdit, useSession } from "./session";

export function RequireSession() {
  const { data: session, isPending, isError } = useSession();
  const location = useLocation();

  if (isPending) return <p className="p-8 text-fg-muted">Завантаження…</p>;
  if (isError) return <p className="p-8 text-danger">Адмін-API недоступний. Оновіть сторінку пізніше.</p>;
  if (!session) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/admin/login?next=${next}`} replace />;
  }
  return (
    <SessionContext value={session}>
      <Outlet />
    </SessionContext>
  );
}

export function RequireAdmin() {
  if (!useCanEdit()) {
    return (
      <div className="rounded-lg border border-line bg-surface p-6">
        <h1 className="text-lg font-semibold">Немає доступу</h1>
        <p className="mt-2 text-sm text-fg-muted">Ця сторінка доступна лише адміністраторам.</p>
      </div>
    );
  }
  return <Outlet />;
}

export function AdminOnly({ children }: { children: ReactNode }) {
  return useCanEdit() ? children : null;
}
