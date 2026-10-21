// All client routes. admin-api serves index.html for any path under /admin/ and /demo/
// (admin_api.spa), so these are the only prefixes; the API owns the rest.
//
// Every page is its own chunk (`lazy`), so the public demo chat never downloads the admin,
// and the admin loads a page's code on first visit. The entry keeps only what every route
// needs: React, the router, TanStack Query, the API client and the session guards.
// scripts/check-bundle.mjs fails the build when that stops being true.
import type { ComponentType } from "react";
import { Navigate, type LazyRouteFunction, type RouteObject } from "react-router";

import { RequireAdmin, RequireSession } from "./admin/guards";
import { RouteError } from "./RouteError";
import { loadChunk } from "./staleChunks";

// A route whose component is the named export of a lazily imported module. A chunk the
// current deploy no longer has reloads the page once (staleChunks.ts).
function page<M>(load: () => Promise<M>, pick: (module: M) => ComponentType): LazyRouteFunction<RouteObject> {
  return async () => ({ Component: pick(await loadChunk(load)) });
}

function Loading() {
  return <p className="p-8 text-sm text-fg-muted">Завантаження…</p>;
}

export const routes: RouteObject[] = [
  {
    // Shown while the first page's chunk loads.
    HydrateFallback: Loading,
    // Any error below, instead of React Router's developer screen.
    ErrorBoundary: RouteError,
    children: [
      { path: "/demo/*", lazy: page(() => import("./demo/DemoPage"), (m) => m.DemoPage) },
      { path: "/admin/login", lazy: page(() => import("./admin/LoginPage"), (m) => m.LoginPage) },
      {
        path: "/admin",
        Component: RequireSession,
        children: [
          {
            lazy: page(() => import("./admin/AdminLayout"), (m) => m.AdminLayout),
            children: [
              { index: true, element: <Navigate to="bots" replace /> },
              { path: "bots", lazy: page(() => import("./admin/bots/BotsPage"), (m) => m.BotsPage) },
              { path: "bots/:botId", lazy: page(() => import("./admin/bots/BotPage"), (m) => m.BotPage) },
              {
                path: "bots/:botId/versions/:a/diff/:b",
                lazy: page(() => import("./admin/bots/DiffPage"), (m) => m.DiffPage),
              },
              {
                path: "bots/:botId/editor",
                lazy: page(() => import("./admin/editor/EditorPage"), (m) => m.EditorPage),
              },
              { path: "calls", lazy: page(() => import("./admin/calls/CallsPage"), (m) => m.CallsPage) },
              { path: "calls/:callId", lazy: page(() => import("./admin/calls/CallPage"), (m) => m.CallPage) },
              { path: "kb", lazy: page(() => import("./admin/kb/KbPage"), (m) => m.KbPage) },
              { path: "ops", lazy: page(() => import("./admin/ops/OpsPage"), (m) => m.OpsPage) },
              { path: "demo", lazy: page(() => import("./admin/demo/DemoChatPage"), (m) => m.DemoChatPage) },
              {
                Component: RequireAdmin,
                children: [
                  {
                    path: "access-codes",
                    lazy: page(() => import("./admin/codes/AccessCodesPage"), (m) => m.AccessCodesPage),
                  },
                ],
              },
              { path: "*", lazy: page(() => import("./admin/NotFound"), (m) => m.NotFound) },
            ],
          },
        ],
      },
      // Only the Vite dev server reaches other paths; admin-api redirects / to /demo/ itself.
      { path: "*", element: <Navigate to="/demo/" replace /> },
    ],
  },
];
