import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the FastAPI process so the browser only ever
// talks to one origin (no CORS in development, no tokens in URLs).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // The preview host is not localhost, so the host allowlist is disabled
    // deliberately for this demo deployment.
    allowedHosts: true,
    proxy: {
      "/api": {
        target: process.env.LEDGERLINE_API_URL ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 4173,
    allowedHosts: true,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
