/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backend routes proxied by the dev server so API calls stay same-origin from the browser.
const BACKEND = process.env.REVEL_BACKEND_URL ?? "http://localhost:8000";
const proxied = ["/auth", "/api", "/me", "/health"];

export default defineConfig({
  // FastAPI serves the built app under /app/ (backend/src/revel/api/ui_static.py).
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(proxied.map((path) => [path, { target: BACKEND }])),
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test-setup.ts"],
    restoreMocks: true,
  },
});
