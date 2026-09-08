# System Interface (alt frontend)

An experimental, independently editable copy of the `system_interface`
frontend (`system/apps/system_interface/frontend`), served as its own app tab.

This is **not** a second workspace: it talks to the exact same running
`system_interface` backend (same agents, same projects, same layout state) at
`http://localhost:8000`. Only the frontend code is forked -- edits here have
no effect on the real workspace UI, and vice versa.

It runs as Vite's own dev server (hot reload on save), not a built bundle:
see `[program:system-interface-alt]` in `system/supervisord.conf`. `/api/*`
(including the `/api/ws` WebSocket) is proxied to the real backend by Vite's
dev-server proxy (`frontend/vite.config.ts`); nothing else talks to it
directly.

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
npm run dev   # already running under supervisord as system-interface-alt
```

Edits under `frontend/src` hot-reload immediately in the open tab.
