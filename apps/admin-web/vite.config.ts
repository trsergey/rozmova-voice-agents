/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The demo page is served by admin-api under /demo/ (ROZ-68). In dev, Vite proxies the API
// to a local admin-api so the session cookie stays on one origin.
const api = "http://localhost:8000";

export default defineConfig({
  base: "/demo/",
  plugins: [react()],
  server: {
    proxy: { "/auth": api, "/bots": api, "/calls": api },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test-setup.ts"],
  },
});
