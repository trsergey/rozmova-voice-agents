// All client routes. admin-api serves index.html for any path under /admin/ and /demo/
// (admin_api.spa), so these are the only prefixes; the API owns the rest.
import { Navigate, type RouteObject } from "react-router";

import { AdminLayout } from "./admin/AdminLayout";
import { BotPage } from "./admin/bots/BotPage";
import { BotsPage } from "./admin/bots/BotsPage";
import { CallPage } from "./admin/calls/CallPage";
import { CallsPage } from "./admin/calls/CallsPage";
import { AccessCodesPage } from "./admin/codes/AccessCodesPage";
import { RequireAdmin, RequireSession } from "./admin/guards";
import { KbPage } from "./admin/kb/KbPage";
import { LoginPage } from "./admin/LoginPage";
import { OpsPage } from "./admin/ops/OpsPage";
import { NotFound } from "./admin/NotFound";
import { DemoPage } from "./demo/DemoPage";

export const routes: RouteObject[] = [
  { path: "/demo/*", element: <DemoPage /> },
  { path: "/admin/login", element: <LoginPage /> },
  {
    path: "/admin",
    element: <RequireSession />,
    children: [
      {
        element: <AdminLayout />,
        children: [
          { index: true, element: <Navigate to="bots" replace /> },
          { path: "bots", element: <BotsPage /> },
          { path: "bots/:botId", element: <BotPage /> },
          { path: "calls", element: <CallsPage /> },
          { path: "calls/:callId", element: <CallPage /> },
          { path: "kb", element: <KbPage /> },
          { path: "ops", element: <OpsPage /> },
          {
            element: <RequireAdmin />,
            children: [{ path: "access-codes", element: <AccessCodesPage /> }],
          },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
  // Only the Vite dev server reaches other paths; admin-api redirects / to /demo/ itself.
  { path: "*", element: <Navigate to="/demo/" replace /> },
];
