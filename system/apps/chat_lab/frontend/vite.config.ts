import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  plugins: [tailwindcss()],
  publicDir: "media",
  root: ".",
  resolve: {
    alias: {
      // Same embed contract the system_interface frontend uses -- see that
      // project's vite.config.ts for why this is vendored rather than duplicated.
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../../vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "dist"),
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 8080,
    strictPort: true,
    // The tab is reached through the workspace forwarder at a per-workspace
    // hostname (not "localhost"), which Vite's dev-server Host-header
    // allowlist rejects by default. The forwarder already restricts what can
    // reach this port, so trusting every Host here doesn't add exposure.
    allowedHosts: true,
    // chat-lab has no backend of its own -- it forks the frontend only and
    // reuses the existing system_interface backend (agent discovery, message
    // send/receive, mngr-managed agent processes) running on :8000.
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        ws: true,
      },
    },
  },
});
