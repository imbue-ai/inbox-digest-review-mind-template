"""Control-plane backend for chat-lab.

The chat-lab UI itself is served by its own Vite dev server (see
system/supervisord.conf's chat-lab program) and reuses system_interface's
existing backend for everything chat-related (agent list, transcripts,
sending messages). This process is a SEPARATE, small backend with exactly
one job: let something else on this machine (a script, another agent) tell
every open chat-lab tab which chat to show.

A connected browser tab opens a WebSocket to ``/ws`` and is added to
``_clients``. ``POST /select-agent`` broadcasts the given agent id to every
connected client, which switches its own view -- no page reload. This is a
POC control channel: no auth, no origin check, in-memory only (a restart
drops all connections, which is fine -- tabs reconnect on their own).

Not registered with forward_port.py: it has no user-facing tab, so it is not
part of ``data/.state/apps.toml`` and is reached only at its localhost port
(see the ``chat-lab-control`` supervisord program), proxied by chat-lab's
Vite dev server at ``/control``.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/chat-lab/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/chat-lab/`` but
  honors the ``CHAT_LAB_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``CHAT_LAB_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
"""

import json
import os
import threading
from pathlib import Path

from flask import Flask, Response, request
from flask_sock import Sock
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/chat-lab/`` but is overridable via the ``CHAT_LAB_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/chat-lab/`` at a call site. Unused today (the
# control channel is in-memory only) but kept for parity with the app
# conventions in case a future change needs to persist anything.
DATA_DIR = Path(os.environ.get("CHAT_LAB_DATA_DIR", "data/.apps/chat-lab"))

# Listen port. Defaults to 8082 (NOT the 8080 the chat-lab tab itself is
# forwarded on -- that port belongs to the Vite dev server) but is
# overridable via the ``CHAT_LAB_PORT`` env var so an editing agent can boot
# a throwaway instance on a spare port alongside the live one.
PORT = int(os.environ.get("CHAT_LAB_PORT", "8082"))

app = Flask("chat_lab", static_folder=None)
sock = Sock(app)

_clients_lock = threading.Lock()
_clients: set = set()


@app.route("/")
def index() -> Response:
    return Response(
        "<!doctype html><html><body>chat-lab control backend -- see /health</body></html>",
        mimetype="text/html",
    )


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


@sock.route("/ws")
def ws(connection) -> None:
    with _clients_lock:
        _clients.add(connection)
    try:
        while True:
            # Clients don't send anything meaningful; blocking on receive is
            # just how we notice a disconnect (raises when the socket closes).
            connection.receive()
    except Exception:
        pass
    finally:
        with _clients_lock:
            _clients.discard(connection)


@app.route("/select-agent", methods=["POST"])
def select_agent() -> Response:
    body = request.get_json(silent=True) or {}
    agent_id = body.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return Response('{"error": "agentId is required"}', status=400, mimetype="application/json")

    message = json.dumps({"type": "select-agent", "agentId": agent_id})
    with _clients_lock:
        clients = list(_clients)
    sent = 0
    for connection in clients:
        try:
            connection.send(message)
            sent += 1
        except Exception:
            with _clients_lock:
                _clients.discard(connection)

    return Response(json.dumps({"sent_to": sent}), mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
