/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// admin-api serves the build: pages under /admin/ and /demo/, the bundle under /assets/.
// In dev, Vite proxies the API to a local admin-api so the session cookie stays on one origin.
const api = "http://localhost:8000";

export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: {
    proxy: { "/auth": api, "/bots": api, "/calls": api, "/kb": api },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test-setup.ts"],
  },
});
