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
