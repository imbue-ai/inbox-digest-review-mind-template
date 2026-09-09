# System Interface (alt frontend)

An experimental, independently editable copy of the `system_interface`
frontend (`system/apps/system_interface/frontend`), served as its own app tab.

This is **not** a second workspace: it talks to the exact same running
`system_interface` backend (same agents, same projects, same layout state) at
`http://localhost:8000`. Only the frontend code is forked -- edits here have
no effect on the real workspace UI, and vice versa.

It runs as **`vite preview`** serving a built bundle (a handful of hashed
files), not `vite dev`'s live source-module server -- see
`[program:system-interface-alt]` in `system/supervisord.conf`. `/api/*`
(including the `/api/ws` WebSocket) is proxied to the real backend by Vite's
proxy (`frontend/vite.config.ts`, both `server.proxy` and `preview.proxy`);
nothing else talks to it directly.

**Why not `vite dev`, given the whole point is to hack on it live:** dev mode
loads the app as ~150 individual on-demand module requests instead of a few
bundled files. That's invisible to a normal browser tab, but the minds
desktop app's local tunnel (`<label>.<agent-id>.localhost:8421`, separate
infrastructure from this container) 503'd partway through under that many
requests -- confirmed the dev server itself wasn't the bottleneck (it handled
150 concurrent requests directly with no trouble), so the fix was fewer
requests, not a faster server. The tradeoff: no hot reload. After editing
`frontend/src`, rebuild and restart to see it:

```bash
cd system/apps/system-interface-alt/frontend
npm run build
supervisorctl restart system-interface-alt
```

If you're editing from *inside* this container (not checking it through the
desktop app), swap the supervisord command back to `npm run dev` for hot
reload -- see the comment above `[program:system-interface-alt]` in
`system/supervisord.conf`.

Because there is no backend of its own rendering `index.html`, none of the
`<meta name="system-interface-*">` tags the real backend injects (base path,
hostname, primary agent id) are present here -- the same limitation the real
frontend's own `npm run dev` workflow already has (see
`system/apps/system_interface/README.md`). Primary-agent-specific UI (e.g.
which chat panel is treated as "the" primary one) degrades accordingly;
everything else -- chats, terminals, browsers, projects, other apps -- works
normally against the shared backend state.

To pull in upstream changes made to the real frontend later, diff
`system/apps/system_interface/frontend` against this directory by hand; there
is no automated sync.

## Development

```bash
cd system/apps/system-interface-alt/frontend
npm run build && supervisorctl restart system-interface-alt
```

For hot reload while iterating from inside this container, run `npm run dev`
directly (a second, throwaway `vite` process on a different port) rather than
switching the supervised one -- that keeps the desktop-app-safe build serving
the registered tab throughout.
