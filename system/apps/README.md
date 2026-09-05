# system/apps/

Apps: everything you can open as a tab in the workspace. Each app is a folder
here -- the built-in ones ship with the template, and apps your mind builds for
you land here too (see the build-app skill). The top-level `apps` symlink
points at this folder.

Built-in apps:

- `system_interface/` - The special one: the workspace UI itself. It hosts the
  tabs the other apps render in, so it is an app that also serves as the
  workspace chrome. Do not use it as a template for new apps.
- `terminal/` - The terminal tab (ttyd over the web), including its named
  persistent sessions.
- `browser/` - The live browser tab: a headless Chromium streamed to the UI.

Python packages in this folder are picked up automatically by the workspace's
`system/apps/*` uv member glob -- no central registration needed beyond the
root `pyproject.toml` dependency the scaffolder adds.

An app usually runs as a supervised service (a `[program:*]` entry in
`system/supervisord.conf`) and registers its port via
`system/scripts/forward_port.py`. An app that needs a continuously running
background component keeps that service's code in its own folder here, named
`<app>-<role>` in supervisord; standalone background services live in
`system/services/` instead.

## External apps (consumed from outside the monorepo)

A folder under `system/apps/` may be a **symlink** into a standalone clone
under `.external_worktrees/<repo>/` (gitignored, see root `.gitignore`).
That is the pattern the external package's own README prescribes when its
"source of truth" lives in another repo (e.g. `check-repos/`, which the
workspace consumes as `system/apps/resource_monitor` ->
`.external_worktrees/check-repos/resource_monitor/`). Keep the symlink
target exactly as the external package author named it; do not edit the
cloned package in place.

Icons for external apps cannot live inside the symlinked package
(modifying it would dirty the external repo). Put them somewhere outside
the `system/apps/*` glob -- the convention in this workspace is
`.app-icons/<app>.svg`, which is referenced from `forward_port.py` and
the supervisord program block via an absolute path. (Putting them in
e.g. `system/apps/_shared_icons/` would match the workspace-member glob
and fail `uv sync` with "missing pyproject.toml".)

## Smoke-checking an app after install

Do NOT run `uv run <app> --help` (or any other argv form) to verify the
entry point works. Most external Flask apps' `main()` calls
`werkzeug.serving.run_simple(...)` unconditionally and ignores argv, so
`--help` just starts the server and blocks forever waiting for SIGTERM.
Use supervisord instead -- `supervisorctl status <name>` and the tail of
`/var/log/supervisor/<name>-stderr.log` are the right smoke; it owns the
process lifecycle and will time out sensibly.
