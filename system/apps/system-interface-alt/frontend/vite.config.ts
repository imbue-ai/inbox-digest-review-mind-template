import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // `dist/` is not part of this project's output -- the bundle goes to
  // `build.outDir` below, and nothing reads `dist/`. It exists only where a
  // checkout still has the emit an older `build` script left behind, and
  // vitest's default include would then collect the compiled COPY of every
  // test beside its source: the same suite twice, with one half frozen at
  // whenever that build ran. Excluded so a stale directory cannot quietly
  // double the count or report passes from code that is no longer there.
  //
  // Added TO vitest's own defaults rather than written out beside them:
  // `exclude` is a whole-list override, so a hand-copied list silently drops
  // whatever else vitest excludes by default and leaves this file owning a
  // decision it has no opinion about. `dist/**` is the only local one.
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  plugins: [tailwindcss()],
  publicDir: "media",
  root: ".",
  resolve: {
    alias: {
      // The minds embed contract -- the single sanctioned postMessage channel
      // between this UI and the embedding minds chrome -- is consumed from the
      // vendored mngr tree so both sides always ship from one source of truth.
      // Types come from src/embed-contract.d.ts; keep the two in sync.
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../../vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  build: {
    // This fork has no backend of its own to hand a bundle to -- it always
    // runs as a live dev server proxying to the real system_interface
    // backend (see server.proxy below), so build output has nowhere
    // meaningful to go. Kept local rather than removed so `npm run build`
    // (used by `lint`/`format:check`'s underlying `tsc --noEmit`) still
    // resolves to a real path instead of clobbering the original app's
    // served bundle.
    outDir: path.resolve(__dirname, "dist"),
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 8095,
    strictPort: true,
    // Reached through the workspace's own label-based origin forwarding,
    // not raw "localhost" -- same trust model as every other app registered
    // via forward_port.py (loopback-bound, gated by the workspace origin),
    // so Vite's Host-header allowlist is disabled rather than enumerated.
    allowedHosts: true,
    proxy: {
      // Same backend, same state as the real system_interface -- this is an
      // alternate frontend only. `ws: true` is required for the API's
      // WebSocket (/api/ws) and per-agent log sockets to proxy through.
      "/api": {
        target: "http://localhost:8000",
        ws: true,
      },
    },
  },
  // Mirrors `server` above so `npm run preview` (serving the built bundle --
  // a handful of hashed files instead of dev mode's ~150 individual on-demand
  // module requests) is a drop-in swap for `npm run dev` when something
  // downstream can't handle that many requests, e.g. the minds desktop app's
  // local tunnel (observed 503s there; the raw dev server had no trouble with
  // the same volume hit directly, pointing at the tunnel rather than this
  // server -- see system-interface-alt/README.md).
  preview: {
    host: "127.0.0.1",
    port: 8095,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        ws: true,
      },
    },
  },
});
