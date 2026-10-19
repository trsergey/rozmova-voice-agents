// All client routes. admin-api serves index.html for any path under /admin/ and /demo/
// (admin_api.spa), so these are the only prefixes; the API owns the rest.
import { Navigate, type RouteObject } from "react-router";

import { AdminLayout } from "./admin/AdminLayout";
import { BotPage } from "./admin/bots/BotPage";
import { BotsPage } from "./admin/bots/BotsPage";
import { RequireAdmin, RequireSession } from "./admin/guards";
import { LoginPage } from "./admin/LoginPage";
import { NotFound, Placeholder } from "./admin/Placeholder";
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
          { path: "calls/*", element: <Placeholder title="Дзвінки" ticket="ROZ-75" /> },
          { path: "kb", element: <Placeholder title="База знань" ticket="ROZ-76" /> },
          {
            element: <RequireAdmin />,
            children: [{ path: "access-codes", element: <Placeholder title="Коди доступу" ticket="ROZ-76" /> }],
          },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
  // Only the Vite dev server reaches other paths; admin-api redirects / to /demo/ itself.
  { path: "*", element: <Navigate to="/demo/" replace /> },
];
