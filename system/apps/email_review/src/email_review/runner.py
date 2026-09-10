"""Email review web service — GTD-aesthetic UI, archive/smart-action endpoints, toast/undo, settings page."""

import base64
import binascii
import html
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import nh3
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from email_review import gmail_actions

ROOT_PATH = os.environ.get("ROOT_PATH", "")
app = FastAPI(title="email-review", root_path=ROOT_PATH)


@app.exception_handler(gmail_actions.GatewayUnreachable)
async def _gateway_unreachable_handler(request: Request, exc: gmail_actions.GatewayUnreachable) -> JSONResponse:
    """Turn a gateway outage into a clean 503 + message instead of a raw 500."""
    return JSONResponse({"detail": str(exc)}, status_code=503)

DATA_PATH = Path("data/.apps/email-review/data.json")
SAVED_PATH = Path("data/.apps/email-review/saved_threads.jsonl")
STARRED_PATH = Path("data/.apps/email-review/starred_threads.jsonl")
# Manual per-thread bucket moves. Applied on top of data.json on every render,
# so a move sticks even after a Refresh re-runs the classifier (user intent wins).
BUCKET_OVERRIDES_PATH = Path("data/.apps/email-review/bucket_overrides.json")
# Append-only log of every manual move — a labeled classification dataset
# (sender, subject, content, classifier bucket, where you moved it) the daily
# review mines for patterns.
MOVE_LOG_PATH = Path("data/.apps/email-review/move_log.jsonl")
# Repo root is resolved from this file's location (.../system/apps/email_review/
# src/email_review/runner.py -> parents[5]) so the refresh subprocesses run with
# the correct cwd regardless of where the repo is checked out.
REPO_ROOT = Path(__file__).resolve().parents[5]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""))


def load_saved_threads() -> list[dict[str, Any]]:
    return _load_jsonl(SAVED_PATH)


def save_thread_entry(entry: dict[str, Any]) -> None:
    existing = [e for e in load_saved_threads() if e.get("thread_id") != entry.get("thread_id")]
    existing.append(entry)
    _write_jsonl(SAVED_PATH, existing)


def unsave_thread_entry(thread_id: str) -> None:
    _write_jsonl(SAVED_PATH, [e for e in load_saved_threads() if e.get("thread_id") != thread_id])


def load_starred_threads() -> list[dict[str, Any]]:
    return _load_jsonl(STARRED_PATH)


def star_thread_entry(entry: dict[str, Any]) -> None:
    existing = [e for e in load_starred_threads() if e.get("thread_id") != entry.get("thread_id")]
    existing.append(entry)
    _write_jsonl(STARRED_PATH, existing)


def unstar_thread_entry(thread_id: str) -> None:
    _write_jsonl(STARRED_PATH, [e for e in load_starred_threads() if e.get("thread_id") != thread_id])

EDITABLE_FILES = [
    ".agents/skills/email-digest/SKILL.md",
    ".agents/skills/email-digest/RULES.md",
    ".agents/skills/email-digest/contacts.txt",
]

BUCKETS = {
    # `smart` = whether to show the per-row smart-action button
    # `bulk`  = which bulk button at the top of the bucket: "smart", "archive", or None
    # archivable=True on the top action-bearing buckets too: you sometimes
    # want to clear a single thread without smart-action / bulk semantics
    # (e.g. you replied elsewhere and just want the row gone). bulk
    # stays None so we never offer "archive all of Reply needed" — that
    # would be terrifying.
    "1":  {"name": "Reply needed",                "annot": "reply",    "archivable": True,  "smart": False, "bulk": None},
    "2":  {"name": "Decision needed",             "annot": "decide",   "archivable": True,  "smart": False, "bulk": None},
    "3":  {"name": "FYI / read",                  "annot": "fyi",      "archivable": True,  "smart": False, "bulk": "archive"},
    "4":  {"name": "TODO",                        "annot": "todo",     "archivable": True,  "smart": False, "bulk": None},
    "5":  {"name": "Sent / awaiting reply",       "annot": "waiting",  "archivable": True,  "smart": False, "bulk": None},
    "6":  {"name": "Cold outreach + event invites","annot": "scan",    "archivable": True,  "smart": True,  "bulk": "smart"},
    "7":  {"name": "Marketing / spam / phishing", "annot": "noise",    "archivable": True,  "smart": True,  "bulk": "smart"},
    # Bucket 8: bulk archive is the right default (most in-product notifications
    # are auto-generated), but a per-row smart button lets you unsubscribe
    # from any specific SaaS notification stream you don't want.
    "8":  {"name": "In-product notifications",    "annot": "noise",    "archivable": True,  "smart": True,  "bulk": "archive"},
    # Reading: per-row smart available for the occasional spam newsletter,
    # but the bulk button is "archive all" since most reading is "skim and
    # archive," not "unsubscribe."
    "9":  {"name": "Reading",                     "annot": "reading",  "archivable": True,  "smart": True,  "bulk": "archive"},
    "10": {"name": "Work FYI",                    "annot": "org",      "archivable": True,  "smart": False, "bulk": "archive"},
    "?":  {"name": "Ambiguous",                   "annot": "ambig",    "archivable": False, "smart": False, "bulk": None},
}

CSS = """
:root {
    --bg: #fafaf7;
    --bg-hover: #f3f0e8;
    --panel: #ffffff;
    --ink: #1c1b1a;
    --ink-soft: #5b554c;
    --ink-faint: #918878;
    --rule: #e8e6e0;
    --rule-soft: #f0ede5;
    --accent: #2d4a3e;
    --accent-soft: #dde7df;
    --accent-action: #b8431f;
    --accent-warn: #8a6a2c;
    --accent-done: #4a6843;
    --font-body: -apple-system, BlinkMacSystemFont, "Inter", system-ui, sans-serif;
    --font-mono: ui-monospace, "SF Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
body {
    margin: 0; color: var(--ink); background: var(--bg);
    font-family: var(--font-body); font-size: 13px; line-height: 1.4;
}

.shell { max-width: 900px; margin: 0 auto; padding: 24px 24px 60px; }

/* ---- masthead ---- */
.masthead {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 16px; padding-bottom: 8px; margin-bottom: 16px;
    border-bottom: 1px solid var(--rule);
}
.masthead__brand { font-size: 18px; font-weight: 600; color: var(--ink); letter-spacing: -0.01em; }
.masthead__date { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); margin-left: 8px; }
.masthead__nav { display: flex; gap: 12px; align-items: baseline; }
.masthead__nav a {
    font-size: 12px; color: var(--accent); text-decoration: none;
    border-bottom: 1px dotted transparent;
}
.masthead__nav a:hover { border-bottom-color: var(--accent); }
.nav-btn {
    font-family: var(--font-body); font-size: 12px;
    color: var(--accent); background: transparent;
    border: 1px solid var(--accent); border-radius: 100px;
    padding: 2px 10px; cursor: pointer;
    transition: background 120ms ease, color 120ms ease;
}
.nav-btn:hover:not(:disabled) { background: var(--accent); color: var(--bg); }
.nav-btn:disabled { opacity: 0.5; cursor: progress; }

/* ---- summary bar ---- */
.summary {
    display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: baseline;
    margin: 0 0 20px; font-size: 12px; color: var(--ink-faint);
}
.summary b { color: var(--ink); font-weight: 600; }
.summary a {
    color: var(--ink-soft); text-decoration: none;
    padding: 2px 8px; border: 1px solid var(--rule); border-radius: 100px; font-size: 11px;
}
.summary a:hover { background: var(--bg-hover); border-color: var(--ink-faint); }

/* ---- bucket sections ---- */
.bucket { margin: 0 0 20px; }
.bucket__head {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 0; border-bottom: 1px solid var(--rule); margin-bottom: 4px;
    /* Left stripe in the bucket's accent — quiet but consistent. */
    border-left: 3px solid var(--accent-color, var(--rule));
    padding-left: 8px;
}
.bucket--reply  { --accent-color: var(--accent-action); }
.bucket--decide { --accent-color: var(--accent-action); }
.bucket--todo   { --accent-color: var(--accent-warn); }
.bucket--waiting{ --accent-color: var(--accent-warn); }
.bucket--scan   { --accent-color: var(--accent); }
.bucket--reading{ --accent-color: var(--accent); }
.bucket--org    { --accent-color: var(--rule); }
.bucket--noise  { --accent-color: var(--rule); }
.bucket--fyi    { --accent-color: var(--rule); }
.bucket__title { font-size: 14px; font-weight: 600; color: var(--ink); }
.bucket__count { font-size: 11px; color: var(--ink-faint); font-family: var(--font-mono); }
.bucket__bulk {
    margin-left: auto; font-family: var(--font-body); font-size: 11px;
    color: var(--accent); background: transparent;
    border: 1px solid var(--accent); border-radius: 100px;
    padding: 2px 10px; cursor: pointer;
    transition: background 140ms ease, color 140ms ease;
}
.bucket__bulk:hover:not(:disabled) { background: var(--accent); color: var(--bg); }
.bucket__bulk:disabled { opacity: 0.5; cursor: progress; }
.bucket__empty { color: var(--ink-faint); font-style: italic; font-size: 12px; padding: 4px 8px; }

/* ---- thread rows ---- */
.thread {
    border-bottom: 1px solid var(--rule-soft);
    padding: 6px 8px; padding-left: 11px;
    display: grid; grid-template-columns: 1fr auto; align-items: baseline; gap: 8px;
    transition: opacity 180ms ease, background 120ms ease;
}
.thread:hover { background: var(--bg-hover); }
.thread__main {
    cursor: pointer; background: none; border: none; padding: 0;
    text-align: left; font: inherit; color: inherit;
    display: flex; align-items: baseline; gap: 6px; min-width: 0;
}
.thread__synth {
    font-size: 13px; color: var(--ink); line-height: 1.35;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.thread.open .thread__synth { white-space: normal; }
.thread__count {
    font-family: var(--font-mono); font-size: 10px; color: var(--ink-faint);
    background: var(--rule-soft); padding: 1px 6px; border-radius: 100px;
    flex-shrink: 0;
}
.thread__result {
    font-family: var(--font-mono); font-size: 10px; color: var(--accent);
    flex-shrink: 0;
}
.thread__result:empty { display: none; }
.thread.acted .thread__result { color: var(--ink-faint); }
.thread__actions {
    display: flex; gap: 4px; align-items: center;
    opacity: 0; transition: opacity 120ms ease;
}
.thread:hover .thread__actions,
.thread:focus-within .thread__actions { opacity: 1; }

.action {
    font-family: var(--font-body); font-size: 11px;
    color: var(--accent); background: transparent;
    border: 1px solid var(--accent); border-radius: 100px;
    padding: 1px 8px; cursor: pointer;
    transition: background 120ms ease, color 120ms ease;
}
.action:hover:not(:disabled) { background: var(--accent); color: var(--bg); }
.action:disabled { opacity: 0.5; cursor: progress; }
.action--smart { color: var(--accent-warn); border-color: var(--accent-warn); }
.action--smart:hover:not(:disabled) { background: var(--accent-warn); color: var(--bg); }
.action--save { color: var(--accent-done); border-color: var(--accent-done); }
.action--save:hover:not(:disabled) { background: var(--accent-done); color: var(--bg); }
/* "move to" dropdown — styled to match the pill action buttons. */
.action--move {
    font-family: var(--font-body); font-size: 11px; color: var(--ink-soft);
    background: var(--panel); border: 1px solid var(--rule);
    border-radius: 100px; padding: 1px 6px; cursor: pointer; max-width: 92px;
}
.action--move:hover:not(:disabled) { border-color: var(--ink-faint); color: var(--ink); }
.action--move:disabled { opacity: 0.5; cursor: progress; }

/* Star toggle: unicode glyph in a clickable span (not a <button>, because
   it lives inside the thread__main button — nested buttons are invalid
   HTML). The hollow ☆ is hover-only to match the smart/archive button
   pattern on the right — visually quiet by default, appears on row hover.
   The filled ★ stays always-visible because it's the state indicator AND
   the unstar handle inside the Starred section. Sits to the LEFT of the
   synth so it's quick to spot/click when revealed. */
.thread__star {
    display: inline-block;
    padding: 0 4px 0 0; margin: 0;
    cursor: pointer; font-size: 14px; line-height: 1;
    color: var(--ink-faint); flex-shrink: 0;
    transition: color 120ms ease, transform 120ms ease, opacity 120ms ease;
    user-select: none;
    opacity: 0;
}
.thread:hover .thread__star,
.thread:focus-within .thread__star { opacity: 1; }
.thread__star:hover { color: var(--accent-warn); transform: scale(1.15); }
.thread__star--on { color: var(--accent-warn); opacity: 1; }
.thread__star.acting { cursor: progress; opacity: 0.5; }

/* Starred bucket uses the warn accent (gold-ish) — matches the star color
   and signals "you marked this" without screaming. */
.bucket--starred { --accent-color: var(--accent-warn); }

.thread.acting { opacity: 0.55; }
.thread.acting .action { cursor: progress; }
.thread.acted { opacity: 0.4; }
.thread.acted .thread__synth { text-decoration: line-through; color: var(--ink-faint); }
.thread.acted .action { display: none; }
/* The inline `.thread__result` span shows the specific action label
   (unsubscribed / muted / spam / archived / saved). No generic checkmark. */

/* ---- expanded messages ---- */
.messages { grid-column: 1 / -1; margin: 6px 0 4px 14px; display: none; }
.thread.open .messages { display: block; }
.ask { grid-column: 1 / -1; margin: 2px 0 8px 14px; }
.ask__input { width: 100%; box-sizing: border-box; font: inherit; padding: 6px 8px;
  border: 1px solid var(--line, #d8d8d2); border-radius: 4px; resize: vertical; }
.ask__row { margin-top: 4px; display: flex; gap: 8px; align-items: center; }
.ask__hint { font-size: 11px; color: var(--ink-soft); }
.thread--moved { background: rgba(90,150,255,0.12); border-radius: 4px;
  box-shadow: 0 0 0 2px rgba(90,150,255,0.20); }
.msg {
    padding: 8px 12px; margin-bottom: 4px;
    background: var(--panel); border: 1px solid var(--rule-soft);
    border-radius: 4px; font-size: 12px;
}
.msg__head {
    display: flex; justify-content: space-between; gap: 6px;
    font-size: 11px; color: var(--ink-faint); margin-bottom: 2px;
}
.msg__sender { color: var(--ink); font-weight: 500; }
.msg__subject { font-weight: 500; margin: 2px 0 4px; color: var(--ink); }
.msg__snippet { color: var(--ink-soft); line-height: 1.4; white-space: pre-wrap; }
.msg__body { line-height: 1.5; white-space: pre-wrap; word-break: break-word;
  margin-top: 6px; max-height: 32em; overflow-y: auto; }
/* Rendered HTML email body: drop pre-wrap (the markup carries its own
   spacing) and keep the email's content boxed inside the row — links wrap,
   images and tables never overflow the digest's narrow column. */
.msg__body--html { white-space: normal; }
.msg__body--html a { color: var(--accent); text-decoration: underline; word-break: break-word; }
.msg__body--html img { max-width: 100%; height: auto; }
.msg__body--html table { max-width: 100%; border-collapse: collapse; }
.msg__body--html p { margin: 4px 0; }
.msg__body--html blockquote { margin: 6px 0; padding: 2px 12px;
  border-left: 2px solid var(--rule); color: var(--ink-soft); }
.msg__foot { margin-top: 4px; }
.msg__source { font-size: 11px; text-decoration: none; opacity: 0.75; }
.msg__source:hover { text-decoration: underline; opacity: 1; }
.msg__more { display: inline-block; margin-top: 4px; font-size: 12px; cursor: pointer;
  text-decoration: underline; opacity: 0.8; }
.msg__more:hover { opacity: 1; }
.msg__why {
    margin-top: 6px; padding-top: 4px; border-top: 1px dashed var(--rule);
    font-size: 10px; color: var(--ink-faint);
}
.msg__why b { color: var(--accent); font-weight: 600; }

/* ---- toast ---- */
#toast-region {
    position: fixed; bottom: 20px; right: 20px; z-index: 1000;
    display: flex; flex-direction: column; gap: 6px;
}
.toast {
    background: var(--ink); color: var(--bg);
    padding: 8px 14px; border-radius: 4px;
    display: flex; align-items: center; gap: 10px;
    box-shadow: 0 2px 10px rgba(28,27,26,0.18);
    font-size: 12px; min-width: 200px;
    animation: rise 140ms ease-out;
    border-left: 3px solid var(--accent-done);
}
.toast.toast--progress { border-left-color: var(--accent-warn); }
.toast.toast--error { border-left-color: var(--accent-action); }
.toast .undo-btn {
    color: var(--bg); background: none; border: 1px solid var(--ink-faint);
    padding: 1px 8px; border-radius: 100px; cursor: pointer; font-size: 11px;
}
.toast .undo-btn:hover { border-color: var(--bg); }
.toast .close-btn { color: var(--ink-faint); background: none; border: none; cursor: pointer; font-size: 14px; padding: 0; }
@keyframes rise { from { transform: translateY(4px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

/* ---- touch devices ---- */
/* Row actions and the star are revealed on hover, which a touch screen can't
   do — so on any non-hover device keep them permanently visible. */
@media (hover: none) {
    .thread__actions { opacity: 1; }
    .thread__star { opacity: 1; }
}

/* ---- mobile ---- */
@media (max-width: 640px) {
    html { -webkit-text-size-adjust: 100%; }
    body { font-size: 15px; }
    .shell { padding: 12px 12px 48px; }

    .masthead { flex-wrap: wrap; align-items: center; gap: 6px 12px; padding-bottom: 6px; margin-bottom: 12px; }
    .masthead__brand { font-size: 18px; }
    .masthead__date { display: block; margin-left: 0; }
    .masthead__nav { flex-wrap: wrap; gap: 6px; width: 100%; }
    .nav-btn { font-size: 12px; padding: 4px 11px; }

    .summary { font-size: 12px; gap: 5px 12px; margin-bottom: 14px; }
    .summary a { font-size: 11px; }

    .bucket { margin-bottom: 16px; }
    .bucket__head { gap: 6px; flex-wrap: wrap; }
    .bucket__title { font-size: 15px; }
    .bucket__count { font-size: 11px; }
    .bucket__bulk { font-size: 11px; padding: 4px 11px; }

    /* Actions on a single line below the subject (no hover on touch). They
       stay on ONE row — compact pills plus a narrow "move" control — and if a
       row ever has too many to fit, the strip scrolls sideways instead of
       wrapping to a second line. */
    .thread { grid-template-columns: 1fr; gap: 5px; padding: 8px; }
    .thread__synth { font-size: 14px; }
    .thread__actions {
        opacity: 1; flex-wrap: nowrap; gap: 6px;
        overflow-x: auto; scrollbar-width: none;
    }
    .thread__actions::-webkit-scrollbar { display: none; }
    .thread__star { opacity: 1; font-size: 16px; padding-right: 4px; }
    .action, .action--smart, .action--save { font-size: 12px; padding: 4px 10px; flex: 0 0 auto; }
    .action--move { font-size: 12px; padding: 4px 8px; flex: 0 0 auto; width: 76px; max-width: 76px; }

    .msg { font-size: 13px; padding: 8px 10px; }
    .msg__head { font-size: 11px; }
    .msg__body { max-height: none; }

    /* Let expanded content and the ask box use the full width on a phone. */
    .messages, .ask { margin-left: 4px; }

    #toast-region { left: 12px; right: 12px; bottom: 12px; }
    .toast { min-width: 0; }
}
"""

JS = r"""
const PREFIX = window.location.pathname.replace(/\/$/, '');

function showToast(msg, opts = {}) {
    const region = document.getElementById('toast-region');
    const t = document.createElement('div');
    t.className = 'toast' + (opts.error ? ' toast--error' : '');
    const m = document.createElement('span');
    m.textContent = msg;
    m.style.flex = '1';
    t.appendChild(m);
    if (opts.undo) {
        const u = document.createElement('button');
        u.className = 'undo-btn'; u.textContent = 'Undo';
        u.onclick = () => { opts.undo(); if (region.contains(t)) region.removeChild(t); };
        t.appendChild(u);
    }
    const close = document.createElement('button');
    close.className = 'close-btn'; close.textContent = '×';
    close.onclick = () => region.contains(t) && region.removeChild(t);
    t.appendChild(close);
    region.appendChild(t);
    // Default 10s so there's time to hit Undo; errors stay 10s too.
    const ms = opts.persist ? 10000 : 10000;
    setTimeout(() => { region.contains(t) && region.removeChild(t); }, ms);
    return t;
}

function confirmInPage(msg) {
    // window.confirm() is blocked inside the workspace_server iframe (modern
    // browsers suppress confirm dialogs in cross-origin iframes). Inline
    // confirmation using a toast-shaped element instead.
    return new Promise(resolve => {
        const region = document.getElementById('toast-region');
        const t = document.createElement('div');
        t.className = 'toast toast--progress';
        const label = document.createElement('span');
        label.style.flex = '1';
        label.textContent = msg;
        t.appendChild(label);
        const yes = document.createElement('button');
        yes.className = 'undo-btn';
        yes.textContent = 'Confirm';
        yes.onclick = () => { if (region.contains(t)) region.removeChild(t); resolve(true); };
        const no = document.createElement('button');
        no.className = 'close-btn';
        no.textContent = '×';
        no.title = 'Cancel';
        no.onclick = () => { if (region.contains(t)) region.removeChild(t); resolve(false); };
        t.appendChild(yes);
        t.appendChild(no);
        region.appendChild(t);
    });
}

function showProgress(msg) {
    const region = document.getElementById('toast-region');
    const t = document.createElement('div');
    t.className = 'toast toast--progress';
    const label = document.createElement('span');
    label.style.flex = '1';
    label.textContent = msg;
    t.appendChild(label);
    region.appendChild(t);
    return {
        update: (newMsg) => { label.textContent = newMsg; },
        close:  () => { if (region.contains(t)) region.removeChild(t); },
    };
}

async function postJson(path, body = {}, method = 'POST') {
    const r = await fetch(PREFIX + path, {
        method: method,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    if (!r.ok) {
        // Surface the server's error detail (e.g. gateway-down message) instead
        // of a bare status code.
        let detail = '';
        try { detail = (await r.json()).detail || ''; } catch (e) { /* no JSON body */ }
        throw new Error(detail || `${r.status} ${r.statusText}`);
    }
    return r.json();
}

function finalizeRow(row, btn, statusClass) {
    // Both succeed and fail land here so we never leave the cursor in
    // "progress" state.
    row.classList.remove('acting');
    if (btn) { btn.disabled = false; btn.blur(); }
    if (statusClass) row.classList.add(statusClass);
}

const ACTION_LABELS = {
    unsubscribed: 'Unsubscribed + archived',
    muted: 'Muted + archived',
    spam: 'Marked as spam + archived',
    archived: 'Archived',
    saved: 'Saved',
};
const ROW_LABELS = {  // shorter labels for the inline row badge
    unsubscribed: 'unsubscribed',
    muted: 'muted',
    spam: 'spam',
    archived: 'archived',
    saved: 'saved',
};

function markRowResult(row, action) {
    const span = row.querySelector('.thread__result');
    if (span) span.textContent = ROW_LABELS[action] || action;
}

async function moveThread(threadId, sel) {
    // Manual bucket move. Persists server-side (survives Refresh), then we
    // re-render the buckets in place so the row slides to its new section.
    const bucket = sel.value;
    if (!bucket) return;
    const name = sel.options[sel.selectedIndex].text;
    sel.disabled = true;
    try {
        await postJson('/api/move', {thread_id: threadId, bucket: bucket});
        // Re-render WITHOUT the whole-list FLIP (a single move reflows many
        // rows). Instead, highlight just the moved row in its new section.
        await renderBuckets(false);
        const moved = document.querySelector(
            '#bucket-mount .thread[data-thread-id="' + (window.CSS && CSS.escape ? CSS.escape(threadId) : threadId) + '"]'
        );
        if (moved) {
            moved.classList.add('thread--moved');
            moved.scrollIntoView({block: 'center', behavior: 'smooth'});
            setTimeout(() => moved.classList.remove('thread--moved'), 1500);
        }
        showToast('Moved to ' + name);
    } catch (e) {
        sel.disabled = false;
        sel.value = '';
        showToast('Move failed: ' + e.message, {persist: true, error: true});
    }
}

async function archiveThread(threadId, btn) {
    const row = btn.closest('.thread');
    // Saved-section rows: archive the Gmail thread but keep the row fully
    // visible — saved items are "to read later" regardless of inbox state.
    const inSavedSection = !!row.closest('#bsaved');
    row.classList.add('acting');
    btn.disabled = true;
    const prog = showProgress('Archiving…');
    try {
        await postJson('/api/archive', {thread_id: threadId});
        prog.close();
        row.classList.remove('acting');
        if (inSavedSection) {
            btn.disabled = false; btn.blur();
            markRowResult(row, 'archived');
        } else {
            finalizeRow(row, btn, 'acted');
            markRowResult(row, 'archived');
        }
        showToast('Archived', {
            undo: async () => {
                await postJson('/api/undo', {thread_id: threadId, undo_add: ['INBOX'], undo_remove: []});
                if (!inSavedSection) row.classList.remove('acted');
                markRowResult(row, '');
                showToast('Restored');
            }
        });
    } catch (e) {
        prog.close();
        finalizeRow(row, btn, null);
        showToast('Failed: ' + e.message, {persist: true, error: true});
    }
}

async function smartActionThread(threadId, bucket, btn) {
    const row = btn.closest('.thread');
    row.classList.add('acting');
    btn.disabled = true;
    const prog = showProgress('Deciding…');
    try {
        const r = await postJson('/api/smart-action', {thread_id: threadId, bucket: bucket});
        prog.close();
        finalizeRow(row, btn, 'acted');
        markRowResult(row, r.action);
        const label = ACTION_LABELS[r.action] || r.action;
        showToast(label, {
            undo: async () => {
                await postJson('/api/undo', {thread_id: threadId, undo_add: r.undo_add, undo_remove: r.undo_remove});
                row.classList.remove('acted');
                markRowResult(row, '');
                showToast('Restored');
            }
        });
    } catch (e) {
        prog.close();
        finalizeRow(row, btn, null);
        showToast('Failed: ' + e.message, {persist: true, error: true});
    }
}

async function refreshSavedSection() {
    // The saved section lives inside a stable #saved-mount div. Just
    // overwrite its innerHTML with whatever the server renders. When the
    // file is empty the server returns "" → the mount goes empty too →
    // the section visually disappears. When non-empty it shows.
    const r = await fetch(PREFIX + '/api/saved-section');
    const html = await r.text();
    const mount = document.getElementById('saved-mount');
    if (mount) mount.innerHTML = html;
}

async function saveThread(threadId, btn) {
    const row = btn.closest('.thread');
    row.classList.add('acting');
    btn.disabled = true;
    const prog = showProgress('Saving…');
    try {
        await postJson('/api/save-thread', {thread_id: threadId});
        await refreshSavedSection();
        prog.close();
        row.classList.remove('acting');
        btn.disabled = false; btn.blur();
        markRowResult(row, 'saved');
        showToast('Saved — see "Saved to read later" at the bottom', {
            undo: async () => {
                await postJson('/api/unsave-thread', {thread_id: threadId});
                await refreshSavedSection();
                markRowResult(row, '');
                showToast('Unsaved');
            }
        });
    } catch (e) {
        prog.close();
        row.classList.remove('acting');
        btn.disabled = false;
        showToast('Failed: ' + e.message, {persist: true, error: true});
    }
}

async function unsaveThread(threadId, btn) {
    const row = btn.closest('.thread');
    row.classList.add('acting');
    btn.disabled = true;
    try {
        await postJson('/api/unsave-thread', {thread_id: threadId});
        row.classList.add('acted');
        markRowResult(row, 'removed');
        await refreshSavedSection();
        showToast('Removed from Saved');
    } catch (e) {
        row.classList.remove('acting');
        btn.disabled = false;
        showToast('Failed: ' + e.message, {persist: true, error: true});
    }
}

async function refreshStarredSection() {
    const r = await fetch(PREFIX + '/api/starred-section');
    const html = await r.text();
    const mount = document.getElementById('starred-mount');
    if (mount) mount.innerHTML = html;
}

function naturalBucketRow(threadId) {
    // The thread's row in its natural bucket (outside #bstarred). Returns
    // null if the thread is no longer in the snapshot (e.g. archived).
    for (const r of document.querySelectorAll(`.thread[data-thread-id="${CSS.escape(threadId)}"]`)) {
        if (!r.closest('#bstarred')) return r;
    }
    return null;
}

async function toggleStar(threadId, btn) {
    // Star toggle: figure out current state from the data attribute, call
    // the matching endpoint, and re-render the Starred section. We also
    // hide/show the natural-bucket row to avoid double-display.
    // btn is a <span role="button">, not a real button, so .disabled doesn't
    // exist — use a CSS class for pending state instead.
    if (btn.classList.contains('acting')) return;
    const wasStarred = btn.dataset.starred === 'true';
    btn.classList.add('acting');
    try {
        if (wasStarred) {
            await postJson('/api/unstar-thread', {thread_id: threadId});
            await refreshStarredSection();
            const natural = naturalBucketRow(threadId);
            if (natural) {
                natural.style.display = '';
                // Reset the natural-row star button so it shows ☆ again.
                const natStar = natural.querySelector('.thread__star');
                if (natStar) {
                    natStar.dataset.starred = 'false';
                    natStar.textContent = '☆';
                    natStar.classList.remove('thread__star--on');
                    natStar.title = 'Star — move to Starred bucket at top';
                }
            } else {
                // The thread was starred in an earlier session, so there's no
                // hidden natural-bucket row to reveal. Re-render the inbox in
                // place so it reappears in its original bucket instead of
                // vanishing until the next reload.
                await renderBuckets(false);
            }
            showToast('Unstarred', {
                undo: async () => {
                    await postJson('/api/star-thread', {thread_id: threadId});
                    if (natural) {
                        natural.style.display = 'none';
                        await refreshStarredSection();
                    } else {
                        await renderBuckets(false);
                    }
                    showToast('Re-starred');
                }
            });
        } else {
            await postJson('/api/star-thread', {thread_id: threadId});
            const natural = naturalBucketRow(threadId);
            if (natural) natural.style.display = 'none';
            await refreshStarredSection();
            showToast('Starred — see top of digest', {
                undo: async () => {
                    await postJson('/api/unstar-thread', {thread_id: threadId});
                    if (natural) {
                        natural.style.display = '';
                        const natStar = natural.querySelector('.thread__star');
                        if (natStar) {
                            natStar.dataset.starred = 'false';
                            natStar.textContent = '☆';
                            natStar.classList.remove('thread__star--on');
                            natStar.title = 'Star — move to Starred bucket at top';
                        }
                    }
                    await refreshStarredSection();
                    showToast('Unstarred');
                }
            });
        }
    } catch (e) {
        showToast('Failed: ' + e.message, {persist: true, error: true});
    } finally {
        btn.classList.remove('acting');
    }
}

async function bulkBucket(bucketId, smart, btn) {
    // Never bulk-act on starred threads. Starring only HIDES the thread's row
    // in its natural bucket (display:none) — the row is still in #b{bucketId} —
    // so exclude any thread currently shown in the Starred section. Starred
    // emails must stay in the inbox even when their bucket is "archive all"-ed.
    const starredIds = new Set(
        Array.from(document.querySelectorAll('#bstarred .thread[data-thread-id]'))
            .map((t) => t.dataset.threadId)
    );
    const rows = Array.from(document.querySelectorAll(`#b${bucketId} .thread:not(.acted)`))
        .filter((r) => !starredIds.has(r.dataset.threadId));
    if (!rows.length) { showToast('Nothing to do'); return; }
    const ok = await confirmInPage(`Apply ${smart ? 'smart action' : 'archive'} to ${rows.length} thread${rows.length > 1 ? 's' : ''}?`);
    if (!ok) return;
    btn.disabled = true;
    const total = rows.length;
    const counts = {unsubscribed: 0, muted: 0, spam: 0, archived: 0};
    let done = 0, fail = 0;
    const prog = showProgress(`Processing 0/${total}…`);
    for (const row of rows) {
        const threadId = row.dataset.threadId;
        row.classList.add('acting');
        try {
            const r = smart
                ? await postJson('/api/smart-action', {thread_id: threadId, bucket: bucketId})
                : await postJson('/api/archive', {thread_id: threadId});
            const action = r.action || 'archived';
            counts[action] = (counts[action] || 0) + 1;
            row.classList.remove('acting');
            row.classList.add('acted');
            markRowResult(row, action);
            done++;
        } catch (e) {
            row.classList.remove('acting');
            fail++;
        }
        // Update progress toast in-place after each item so the user sees motion
        const tally = Object.entries(counts).filter(([_, n]) => n > 0)
            .map(([k, n]) => `${n} ${ACTION_LABELS[k] || k}`).join(', ');
        prog.update(`Processing ${done + fail}/${total}` + (tally ? ` — ${tally}` : ''));
    }
    prog.close();
    btn.disabled = false; btn.blur();
    const summary = Object.entries(counts).filter(([_, n]) => n > 0)
        .map(([k, n]) => `${n} ${k}`).join(', ') || 'nothing';
    showToast(`Done: ${summary}${fail ? ` (${fail} failed)` : ''}`, {persist: true, error: fail > 0});
}

async function loadMessageBody(msgEl) {
    if (msgEl.dataset.loaded) return;
    const id = msgEl.dataset.msgId;
    if (!id) return;
    msgEl.dataset.loaded = '1';
    const bodyEl = msgEl.querySelector('.msg__body');
    const snipEl = msgEl.querySelector('.msg__snippet');
    const moreEl = msgEl.querySelector('.msg__more');
    if (!bodyEl) return;
    if (moreEl) moreEl.hidden = true;
    bodyEl.hidden = false;
    bodyEl.textContent = 'Loading full message…';
    try {
        const r = await fetch(PREFIX + '/api/message/' + encodeURIComponent(id));
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        const data = await r.json();
        const htmlBody = (data.html || '').trim();
        if (htmlBody) {
            // The server already sanitized this (nh3), so innerHTML is safe.
            // Force links to open in a new tab — the digest lives in an iframe.
            bodyEl.innerHTML = htmlBody;
            bodyEl.classList.add('msg__body--html');
            bodyEl.querySelectorAll('a[href]').forEach((a) => {
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
            });
            if (snipEl) snipEl.hidden = true;
        } else {
            bodyEl.textContent = '(no readable text body — use “open in Gmail”)';
        }
    } catch (e) {
        // Restore the snippet + "show full message" link so the user can retry.
        bodyEl.hidden = true;
        bodyEl.textContent = '';
        msgEl.dataset.loaded = '';
        if (moreEl) moreEl.hidden = false;
        showToast('Couldn’t load the full message', {error: true});
    }
}

function toggleThread(el) {
    const thread = el.closest('.thread');
    thread.classList.toggle('open');
}

function toggleAskBox(btn) {
    const thread = btn.closest('.thread');
    const box = thread.querySelector('.ask');
    if (!box) return;
    box.hidden = !box.hidden;
    if (!box.hidden) {
        thread.classList.add('open');
        const input = box.querySelector('.ask__input');
        if (input) input.focus();
    }
}

function buildThreadContext(thread) {
    const tid = thread.dataset.threadId || '';
    const bucket = thread.dataset.bucket || '';
    const synthEl = thread.querySelector('.thread__synth');
    const summary = synthEl ? synthEl.textContent.trim() : '';
    const msgs = Array.from(thread.querySelectorAll('.msg[data-msg-id]'));
    const ids = msgs.map(m => m.dataset.msgId).filter(Boolean);
    const top = msgs[0];  // most recent (rendered first)
    const pick = (sel) => (top && top.querySelector(sel)) ? top.querySelector(sel).textContent.trim() : '';
    const subject = pick('.msg__subject');
    const from = pick('.msg__sender');
    let date = '';
    if (top) {
        const spans = top.querySelectorAll('.msg__head span');
        if (spans.length > 1) date = spans[1].textContent.trim();
    }
    const anchor = ids[0] || tid;
    const gmail = anchor ? ('https://mail.google.com/mail/u/0/#all/' + anchor) : '';
    return {tid, bucket, summary, subject, from, date, ids, gmail};
}

function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    showToast(ok ? 'Copied — paste into your Mind chat' : 'Copy failed — select the text manually', {error: !ok});
}

async function copyAskForChat(btn) {
    const thread = btn.closest('.thread');
    const box = btn.closest('.ask');
    const input = box.querySelector('.ask__input');
    const instruction = (input.value || '').trim();
    if (!instruction) {
        showToast('Type an instruction first', {error: true});
        input.focus();
        return;
    }
    const c = buildThreadContext(thread);
    const block = [
        '[email-review · action request]',
        'Thread: "' + c.subject + '"',
        'From: ' + c.from + ' · ' + c.date + ' · bucket ' + c.bucket,
        'Thread-ID: ' + c.tid + '   Messages: ' + c.ids.join(', '),
        'Open in Gmail: ' + c.gmail,
        'Summary: ' + c.summary,
        '',
        'My instruction:',
        instruction,
        '',
    ].join('\n');
    try {
        await navigator.clipboard.writeText(block);
        showToast('Copied — paste into your Mind chat');
    } catch (e) {
        fallbackCopy(block);
    }
}

function _setNum(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }

function applySummary(v) {
    _setNum('sum-msgs', v.total_msgs);
    _setNum('sum-threads', v.total_threads);
    _setNum('sum-keep', v.keep);
    _setNum('sum-archive', v.archive);
    const jm = document.getElementById('jump-mount');
    if (jm) jm.innerHTML = v.jump;
}

function _threadRects() {
    const r = {};
    document.querySelectorAll('#bucket-mount .thread[data-thread-id]').forEach((t) => {
        r[t.dataset.threadId] = t.getBoundingClientRect();
    });
    return r;
}

// Re-render the bucket section from the server in place. When animate=true,
// FLIP-animate any thread that changed position (i.e. moved bucket) so the
// judge's re-classification visibly slides into place instead of reloading.
async function renderBuckets(animate) {
    const mount = document.getElementById('bucket-mount');
    if (!mount) { window.location.reload(); return; }
    const before = animate ? _threadRects() : null;
    let v;
    try {
        const r = await fetch(PREFIX + '/api/buckets');
        if (!r.ok) throw new Error(r.status);
        v = await r.json();
    } catch (e) { window.location.reload(); return; }
    mount.innerHTML = v.bucket_html;
    applySummary(v);
    if (!animate || !before) return;
    const moved = [];
    document.querySelectorAll('#bucket-mount .thread[data-thread-id]').forEach((t) => {
        const old = before[t.dataset.threadId];
        if (!old) return;                                  // newly-appeared thread
        const now = t.getBoundingClientRect();
        const dx = old.left - now.left, dy = old.top - now.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;  // didn't move
        t.style.transition = 'none';
        t.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
        moved.push(t);
    });
    if (!moved.length) return;
    void document.body.offsetWidth;                        // flush the inverted positions
    requestAnimationFrame(() => {
        moved.forEach((t) => {
            t.style.transition = 'transform 480ms cubic-bezier(.2,.7,.2,1)';
            t.style.transform = '';
            t.classList.add('thread--moved');
        });
        setTimeout(() => moved.forEach((t) => {
            t.style.transition = '';
            t.classList.remove('thread--moved');
        }), 1500);
    });
}

async function refreshDigest(btn) {
    btn.disabled = true;
    const prog = showProgress('Refreshing inbox… (takes ~15-30s)');
    const STEP_LABEL = {
        queued: 'Queued',
        propagate_mutes: 'Propagating muted threads',
        classify: 'Classifying inbox',
        synthesize: 'Synthesizing summaries',
    };
    try {
        const r = await fetch(PREFIX + '/api/refresh', {method: 'POST'});
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        while (true) {
            await new Promise((res) => setTimeout(res, 2000));
            const sr = await fetch(PREFIX + '/api/refresh-status');
            if (!sr.ok) throw new Error('status poll: ' + sr.status);
            const s = await sr.json();
            if (s.in_progress) {
                prog.update((STEP_LABEL[s.step] || s.step || 'Working') + '…');
                continue;
            }
            if (s.error) throw new Error(s.error);
            if (!s.result) throw new Error('no result returned');
            prog.close();
            showToast(`Refreshed ${s.result.messages} messages.`);
            await renderBuckets(false);
            btn.disabled = false;
            return;
        }
    } catch (e) {
        prog.close();
        btn.disabled = false;
        showToast('Refresh failed: ' + e.message, {persist: true, error: true});
    }
}

async function categorizeDigest(btn) {
    btn.disabled = true;
    const prog = showProgress('LLM judge self-review… (takes ~30-60s)');
    try {
        const r = await fetch(PREFIX + '/api/categorize', {method: 'POST'});
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        while (true) {
            await new Promise((res) => setTimeout(res, 2000));
            const sr = await fetch(PREFIX + '/api/categorize-status');
            if (!sr.ok) throw new Error('status poll: ' + sr.status);
            const s = await sr.json();
            if (s.in_progress) {
                prog.update('Reviewing reply-needed and FYI threads…');
                continue;
            }
            if (s.error) throw new Error(s.error);
            if (!s.result) throw new Error('no result returned');
            prog.close();
            showToast(`Categorize: ${s.result.llm_judge_moves} move(s) across ${s.result.messages} messages.`);
            await renderBuckets(true);
            btn.disabled = false;
            return;
        }
    } catch (e) {
        prog.close();
        btn.disabled = false;
        showToast('Categorize failed: ' + e.message, {persist: true, error: true});
    }
}

async function refreshAndCategorize(btn) {
    btn.disabled = true;
    const prog = showProgress('Refreshing inbox… (this takes ~2 min total)');
    const REFRESH_STEPS = {
        queued: 'Queued',
        propagate_mutes: 'Propagating muted threads',
        classify: 'Classifying inbox',
        synthesize: 'Synthesizing summaries',
    };
    try {
        // Step 1: refresh, poll until done.
        let r = await fetch(PREFIX + '/api/refresh', {method: 'POST'});
        if (!r.ok) throw new Error('refresh start: ' + r.status);
        while (true) {
            await new Promise((res) => setTimeout(res, 2000));
            const sr = await fetch(PREFIX + '/api/refresh-status');
            if (!sr.ok) throw new Error('refresh poll: ' + sr.status);
            const s = await sr.json();
            if (s.in_progress) {
                prog.update((REFRESH_STEPS[s.step] || s.step || 'Working') + '…');
                continue;
            }
            if (s.error) throw new Error('refresh: ' + s.error);
            if (!s.result) throw new Error('refresh: no result returned');
            break;
        }
        // Show the freshly rule-classified inbox immediately (no blank screen),
        // then let the judge's moves animate in.
        await renderBuckets(false);
        // Step 2: categorize, poll until done.
        prog.update('LLM judge refining reply-needed / FYI… (~100s; items will move)');
        r = await fetch(PREFIX + '/api/categorize', {method: 'POST'});
        if (!r.ok) throw new Error('categorize start: ' + r.status);
        let catResult = null;
        while (true) {
            await new Promise((res) => setTimeout(res, 2000));
            const sr = await fetch(PREFIX + '/api/categorize-status');
            if (!sr.ok) throw new Error('categorize poll: ' + sr.status);
            const s = await sr.json();
            if (s.in_progress) {
                prog.update('Categorizing reply-needed and FYI threads…');
                continue;
            }
            if (s.error) throw new Error('categorize: ' + s.error);
            if (!s.result) throw new Error('categorize: no result returned');
            catResult = s.result;
            break;
        }
        prog.close();
        showToast(`Refreshed + categorized — ${catResult.llm_judge_moves} judge move(s) across ${catResult.messages} messages.`);
        await renderBuckets(true);
        btn.disabled = false;
    } catch (e) {
        prog.close();
        btn.disabled = false;
        showToast('Refresh + Categorize failed: ' + e.message, {persist: true, error: true});
    }
}
"""


def parse_date(s: str) -> datetime:
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if not s:
        return EPOCH
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return EPOCH
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class _HtmlToText(HTMLParser):
    """Minimal HTML-to-text fallback for emails that have no text/plain part.

    Plain text is always preferred; this only runs for HTML-only messages. It
    drops script/style content and inserts line breaks at block boundaries. The
    result is rendered as plain text (white-space: pre-wrap), never as HTML, so
    untrusted email markup is not an XSS surface.
    """

    _BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def _collapse_blank_lines(text: str) -> str:
    out: list[str] = []
    blanks = 0
    for line in text.splitlines():
        if line.strip():
            blanks = 0
            out.append(line.rstrip())
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def _html_to_text(markup: str) -> str:
    parser = _HtmlToText()
    parser.feed(markup)
    return _collapse_blank_lines(html.unescape(parser.get_text()))


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except binascii.Error:
        return ""
    return raw.decode("utf-8", "replace")


def _collect_body_parts(
    payload: dict[str, Any], plains: list[str], htmls: list[str]
) -> None:
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime == "text/plain":
        plains.append(_decode_b64url(data))
    elif data and mime == "text/html":
        htmls.append(_decode_b64url(data))
    for part in payload.get("parts") or []:
        _collect_body_parts(part, plains, htmls)


def extract_message_body(payload: dict[str, Any]) -> tuple[str, str]:
    """Pull a readable text body out of a Gmail ``format=full`` payload.

    Prefers the text/plain part(s); falls back to a tag-stripped text version
    of the HTML part(s). Returns ``(text, source_mime)``.
    """
    plains: list[str] = []
    htmls: list[str] = []
    _collect_body_parts(payload, plains, htmls)
    plain = "\n".join(p for p in plains if p.strip()).strip()
    if plain:
        return _collapse_blank_lines(plain), "text/plain"
    html_joined = "\n".join(h for h in htmls if h.strip())
    if html_joined:
        return _html_to_text(html_joined), "text/html"
    return "", "none"


# Cloudflare email-address obfuscation. Sites behind Cloudflare with "Email
# Address Obfuscation" on rewrite every address into an element carrying the
# real address hex-encoded in `data-cfemail` (and a `/cdn-cgi/l/email-protection`
# href), showing the literal placeholder text "[email protected]". Cloudflare's
# own JS decodes it in the browser; we don't run that JS, so we decode it here
# before sanitizing — otherwise the reader just sees the placeholder.
_CF_EMAIL_ELEMENT_RE = re.compile(
    r'<(?P<tag>\w+)\b[^>]*\bdata-cfemail="(?P<hex>[0-9a-fA-F]+)"[^>]*>.*?</(?P=tag)>',
    re.IGNORECASE | re.DOTALL,
)
_CF_EMAIL_HREF_RE = re.compile(
    r'href="[^"]*?/cdn-cgi/l/email-protection#(?P<hex>[0-9a-fA-F]+)[^"]*"',
    re.IGNORECASE,
)


def _decode_cfemail(hex_str: str) -> str:
    """Decode a Cloudflare `data-cfemail` hex string to the real address.

    The first byte is an XOR key; each remaining byte XORed with it yields one
    ASCII character of the address. Returns "" if the hex is malformed or the
    result isn't a plausible address, so the caller can leave the markup alone.
    """
    if len(hex_str) < 4 or len(hex_str) % 2:
        return ""
    data = bytes.fromhex(hex_str)
    key = data[0]
    addr = "".join(chr(b ^ key) for b in data[1:])
    if "@" not in addr or not addr.isascii() or not addr.isprintable():
        return ""
    return addr


def _deobfuscate_cf_emails(markup: str) -> str:
    """Replace Cloudflare-obfuscated addresses with real `mailto:` links."""

    def replace_element(m: re.Match[str]) -> str:
        addr = _decode_cfemail(m.group("hex"))
        if not addr:
            return m.group(0)
        esc = html.escape(addr)
        return f'<a href="mailto:{esc}">{esc}</a>'

    def replace_href(m: re.Match[str]) -> str:
        addr = _decode_cfemail(m.group("hex"))
        if not addr:
            return m.group(0)
        return f'href="mailto:{html.escape(addr)}"'

    out = _CF_EMAIL_ELEMENT_RE.sub(replace_element, markup)
    return _CF_EMAIL_HREF_RE.sub(replace_href, out)


# Tracking pixels are near-invisible images (typically 1x1) whose only job is
# to phone home when the email is opened. We strip those so opening a message
# doesn't leak a read receipt, while leaving real remote images intact.
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _is_tracking_pixel(img_tag: str) -> bool:
    """True if an ``<img>`` tag looks like a 1x1 (or 0x0) tracking pixel.

    Checks the ``width``/``height`` attributes and any inline ``style`` for a
    dimension of 0 or 1 px. A real image with no declared size, or any size
    above one pixel, is kept.
    """
    for attr in ("width", "height"):
        m = re.search(rf'\b{attr}\s*=\s*["\']?\s*(\d+)', img_tag, re.IGNORECASE)
        if m and int(m.group(1)) <= 1:
            return True
    style_m = re.search(r'style\s*=\s*["\']([^"\']*)["\']', img_tag, re.IGNORECASE)
    if style_m:
        style = style_m.group(1).lower()
        for prop in ("width", "height"):
            dim_m = re.search(rf"{prop}\s*:\s*(\d+)\s*px", style)
            if dim_m and int(dim_m.group(1)) <= 1:
                return True
    return False


def _strip_tracking_pixels(markup: str) -> str:
    """Remove 1x1 tracking-pixel images, keeping all other (real) images."""
    return _IMG_TAG_RE.sub(lambda m: "" if _is_tracking_pixel(m.group(0)) else m.group(0), markup)


# Bare URLs in a plain-text body. Stops at whitespace and the few characters
# that can't legitimately appear unencoded in a URL, so trailing markup or
# quotes don't get swallowed into the link.
_BARE_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def _plaintext_to_html(text: str) -> str:
    """Escape a plain-text body and turn bare URLs into clickable links.

    Newlines are preserved by the ``white-space: pre-wrap`` style on the body
    container, so linkifying is all that's needed here. Escaping happens first
    so the body itself can never inject markup; the only tags we add are the
    anchors we control.
    """
    escaped = html.escape(text)
    return _BARE_URL_RE.sub(r'<a href="\1">\1</a>', escaped)


def sanitize_email_html(markup: str) -> str:
    """Sanitize an email's HTML body for safe in-page rendering.

    nh3 (the Rust ``ammonia`` sanitizer) strips ``<script>``, event handlers,
    and dangerous URL schemes (``javascript:`` etc.), drops ``style``/``class``
    attributes so the email's own CSS can't leak into the digest layout, and
    forces ``rel="noopener noreferrer"`` on links. The result is safe to set
    via ``innerHTML``.
    """
    return nh3.clean(markup)


def extract_message_html(payload: dict[str, Any]) -> tuple[str, str]:
    """Pull a renderable body out of a Gmail ``format=full`` payload as HTML.

    Prefers the HTML part (sanitized) so links and formatting render and are
    clickable; falls back to the text/plain part with bare URLs linkified.
    Returns ``(html, source_mime)``.
    """
    plains: list[str] = []
    htmls: list[str] = []
    _collect_body_parts(payload, plains, htmls)
    html_joined = "\n".join(h for h in htmls if h.strip())
    if html_joined.strip():
        # Decode Cloudflare-obfuscated addresses and drop tracking pixels before
        # sanitizing — the sanitizer strips the `data-cfemail` markup and the
        # `width`/`height`/`style` attributes those steps rely on.
        prepared = _strip_tracking_pixels(_deobfuscate_cf_emails(html_joined))
        return sanitize_email_html(prepared), "text/html"
    plain = "\n".join(p for p in plains if p.strip()).strip()
    if plain:
        return _plaintext_to_html(_collapse_blank_lines(plain)), "text/plain"
    return "", "none"


def render_msg(m: dict[str, Any]) -> str:
    mid = html.escape(str(m.get("id", "")))
    source = (
        f'<a class="msg__source" target="_blank" rel="noopener" '
        f'href="https://mail.google.com/mail/u/0/#all/{mid}">open in Gmail ↗</a>'
        if mid
        else ""
    )
    more = (
        '<a class="msg__more" role="button" tabindex="0" '
        "onclick=\"loadMessageBody(this.closest('.msg')); event.stopPropagation();\">"
        "show full message</a>"
        if mid
        else ""
    )
    return f"""
<div class="msg" data-msg-id="{mid}">
  <div class="msg__head">
    <span class="msg__sender">{html.escape(m.get("from",""))}</span>
    <span>{html.escape(m.get("date","")[:25])}</span>
  </div>
  <div class="msg__subject">{html.escape(m.get("subject",""))}</div>
  <div class="msg__snippet">{html.escape(m.get("snippet",""))}</div>
  {more}
  <div class="msg__body" hidden></div>
  <div class="msg__foot">{source}</div>
  <div class="msg__why"><b>Why this bucket:</b> {html.escape(m.get("final_why",""))}</div>
</div>
"""


def thread_synth(msgs: list[dict[str, Any]]) -> str:
    most_recent = max(msgs, key=lambda m: parse_date(m.get("date", "")))
    return most_recent.get("synthesis") or most_recent.get("subject", "")


def load_bucket_overrides() -> dict[str, str]:
    """Manual per-thread bucket moves, keyed by threadId → bucket id."""
    try:
        return json.loads(BUCKET_OVERRIDES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_bucket_overrides(overrides: dict[str, str]) -> None:
    BUCKET_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUCKET_OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2))


def build_move_record(
    thread_id: str,
    to_bucket: str,
    from_bucket: str,
    msgs: list[dict[str, Any]],
    moved_at: str,
) -> dict[str, Any]:
    """Assemble a labeled move record from a thread's messages — the training
    signal for learning the classifier from manual moves. Uses the most recent
    message for sender/subject/content, and records the classifier's own bucket
    and reasoning alongside where the user moved it."""
    recent = max(msgs, key=lambda m: m.get("date", ""), default={}) if msgs else {}
    return {
        "moved_at": moved_at,
        "thread_id": thread_id,
        "message_id": recent.get("id", ""),
        "from": recent.get("from", ""),
        "from_addr": recent.get("from_addr", ""),
        "to": recent.get("to", ""),
        "cc": recent.get("cc", ""),
        "subject": recent.get("subject", ""),
        "snippet": recent.get("snippet", ""),
        "synthesis": recent.get("synthesis", ""),
        "labels": recent.get("labels", []),
        "cold_check": recent.get("cold_check", ""),
        "classifier_bucket": recent.get("final_bucket", ""),
        "classifier_why": recent.get("final_why", ""),
        "from_bucket": from_bucket,
        "to_bucket": to_bucket,
        "to_bucket_name": BUCKETS.get(to_bucket, {}).get("name", to_bucket),
        "n_messages": len(msgs),
    }


def apply_bucket_overrides(msgs: list[dict[str, Any]], overrides: dict[str, str]) -> None:
    """Re-bucket messages whose thread has a manual override. Mutates in place;
    callers pass a freshly-loaded message list each render, so this is safe and
    makes overrides survive a Refresh (they're re-applied every time)."""
    if not overrides:
        return
    for m in msgs:
        tid = m.get("threadId", m.get("id"))
        if tid in overrides:
            m["final_bucket"] = overrides[tid]


# Buckets offered in the per-row "move to" dropdown, in display order.
_MOVE_TARGET_ORDER = ["1", "2", "4", "5", "6", "3", "9", "10", "7", "8"]


def _move_options_html(current_bucket_id: str) -> str:
    opts = ['<option value="">move…</option>']
    for b in _MOVE_TARGET_ORDER:
        if b == current_bucket_id:
            continue
        opts.append(f'<option value="{b}">{html.escape(BUCKETS[b]["name"])}</option>')
    return "".join(opts)


def render_thread(
    thread_id: str,
    msgs: list[dict[str, Any]],
    bucket_id: str,
    is_starred: bool = False,
) -> str:
    msgs_sorted = sorted(msgs, key=lambda m: parse_date(m.get("date", "")), reverse=True)
    synth = thread_synth(msgs_sorted)
    count_badge = f'<span class="thread__count">{len(msgs)}</span>' if len(msgs) > 1 else ""
    bucket = BUCKETS.get(bucket_id, {})
    # "move" is leftmost; "archive" (appended last below) stays rightmost.
    actions = []
    if not is_starred:
        # "move to" dropdown — manual bucket move, persists across refreshes.
        actions.append(
            f'<select class="action action--move" '
            f"onchange=\"moveThread('{thread_id}', this); event.stopPropagation();\" "
            f'onclick="event.stopPropagation();" title="Move this thread to another bucket">'
            f"{_move_options_html(bucket_id)}</select>"
        )
    actions.append(
        '<button class="action action--ask" '
        "onclick=\"toggleAskBox(this); event.stopPropagation();\" "
        'title="Send an instruction about this thread to your Mind">ask</button>'
    )
    if bucket.get("smart"):
        actions.append(
            f'<button class="action action--smart" '
            f"onclick=\"smartActionThread('{thread_id}','{bucket_id}', this); event.stopPropagation();\" "
            f'title="Decide best action (unsub / mute / spam / archive)">smart</button>'
        )
    if bucket_id == "9":
        # Reading bucket: save-for-later button. Persists to a JSONL file.
        actions.append(
            f'<button class="action action--save" '
            f"onclick=\"saveThread('{thread_id}', this); event.stopPropagation();\" "
            f'title="Save to read later — moves to Saved section">save</button>'
        )
    if bucket.get("archivable"):
        actions.append(
            f'<button class="action" '
            f"onclick=\"archiveThread('{thread_id}', this); event.stopPropagation();\" "
            f'title="Archive thread">archive</button>'
        )
    actions_html = "".join(actions)
    body = "\n".join(render_msg(m) for m in msgs_sorted)
    # Star toggle on every thread row. Filled glyph when starred; clicking
    # calls star/unstar and re-renders both the Starred section and the
    # affected row's icon state. Rendered as a <span> (not <button>) because
    # this element lives inside the thread__main button, and nested buttons
    # are invalid HTML — browsers reorder them, which moved the star to the
    # row's right edge in earlier iterations.
    star_glyph = "★" if is_starred else "☆"
    star_cls = "thread__star thread__star--on" if is_starred else "thread__star"
    star_title = "Unstar" if is_starred else "Star — move to Starred bucket at top"
    star_btn = (
        f'<span class="{star_cls}" data-starred="{str(is_starred).lower()}" '
        f'role="button" tabindex="0" '
        f"onclick=\"toggleStar('{thread_id}', this); event.stopPropagation();\" "
        f'title="{star_title}">{star_glyph}</span>'
    )
    return f"""
<div class="thread" data-thread-id="{thread_id}" data-bucket="{html.escape(bucket_id)}">
  <button class="thread__main" onclick="toggleThread(this)">
    {star_btn}
    <span class="thread__synth">{html.escape(synth)}</span>
    {count_badge}
    <span class="thread__result"></span>
  </button>
  <span class="thread__actions">{actions_html}</span>
  <div class="messages">{body}</div>
  <div class="ask" hidden>
    <textarea class="ask__input" rows="2" placeholder="Tell your Mind what to do with this thread…"></textarea>
    <div class="ask__row">
      <button class="ask__copy" onclick="copyAskForChat(this)">Copy for chat</button>
      <span class="ask__hint">then paste into your Mind chat</span>
    </div>
  </div>
</div>
"""


def render_starred_section() -> str:
    """Render the manually-curated Starred section that sits at the top of
    the digest. Starred threads are pulled OUT of their natural bucket and
    shown here instead (unstar puts them back). Items still in data.json get
    full rendering (with smart-action / archive); items no longer in the
    snapshot fall back to the cached metadata in starred_threads.jsonl.
    """
    starred = load_starred_threads()
    if not starred:
        return ""
    starred.sort(key=lambda e: e.get("starred_at", ""), reverse=True)
    # Pull live message records from data.json so the starred view shows
    # current state (expanded messages, why-this-bucket annotation, etc.).
    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        data = json.loads(DATA_PATH.read_text())
        for m in data.get("messages", []):
            by_thread[m.get("threadId", m["id"])].append(m)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    rows = []
    for entry in starred:
        tid = entry.get("thread_id", "")
        live_msgs = by_thread.get(tid, [])
        if live_msgs:
            # Pass the natural bucket as the rendering bucket so per-row
            # action buttons (smart/archive) match what the bucket allows.
            natural = entry.get("natural_bucket", "?")
            rows.append(render_thread(tid, live_msgs, natural, is_starred=True))
        else:
            # Cached fallback: thread is no longer in the snapshot (it was
            # archived from Gmail elsewhere). Render from the JSONL metadata.
            synth = entry.get("synthesis") or entry.get("subject") or "(starred)"
            starred_at = entry.get("starred_at", "")[:10]
            rows.append(f"""
<div class="thread" data-thread-id="{html.escape(tid)}">
  <button class="thread__main" onclick="toggleThread(this)">
    <span class="thread__star thread__star--on" data-starred="true" role="button" tabindex="0" onclick="toggleStar('{html.escape(tid)}', this); event.stopPropagation();" title="Unstar">★</span>
    <span class="thread__synth">{html.escape(synth)}</span>
    <span class="thread__count">starred {html.escape(starred_at)}</span>
    <span class="thread__result"></span>
  </button>
  <span class="thread__actions">
    <button class="action" onclick="archiveThread('{html.escape(tid)}', this); event.stopPropagation();" title="Archive thread">archive</button>
  </span>
  <div class="messages">
    <div class="msg">
      <div class="msg__head"><span class="msg__sender">{html.escape(entry.get('from',''))}</span></div>
      <div class="msg__subject">{html.escape(entry.get('subject',''))}</div>
      <div class="msg__snippet">{html.escape(entry.get('snippet',''))}</div>
    </div>
  </div>
</div>
""")
    return f"""
<section class="bucket" id="bstarred">
  <div class="bucket__head bucket--starred">
    <span class="bucket__title">★ Starred</span>
    <span class="bucket__count">{len(starred)} thread{'s' if len(starred) != 1 else ''}</span>
  </div>
  {"".join(rows)}
</section>
"""


def render_saved_section() -> str:
    saved = load_saved_threads()
    if not saved:
        return ""
    # Sort newest-saved first
    saved.sort(key=lambda e: e.get("saved_at", ""), reverse=True)
    starred_ids = {e.get("thread_id") for e in load_starred_threads()}
    rows = []
    for entry in saved:
        synth = entry.get("synthesis") or entry.get("subject") or "(saved)"
        tid = entry.get("thread_id", "")
        saved_at = entry.get("saved_at", "")[:10]
        is_starred = tid in starred_ids
        star_glyph = "★" if is_starred else "☆"
        star_cls = "thread__star thread__star--on" if is_starred else "thread__star"
        star_title = "Unstar" if is_starred else "Star — move to Starred bucket at top"
        rows.append(f"""
<div class="thread" data-thread-id="{html.escape(tid)}">
  <button class="thread__main" onclick="toggleThread(this)">
    <span class="{star_cls}" data-starred="{str(is_starred).lower()}" role="button" tabindex="0" onclick="toggleStar('{html.escape(tid)}', this); event.stopPropagation();" title="{star_title}">{star_glyph}</span>
    <span class="thread__synth">{html.escape(synth)}</span>
    <span class="thread__count">saved {html.escape(saved_at)}</span>
  </button>
  <span class="thread__actions">
    <button class="action" onclick="unsaveThread('{html.escape(tid)}', this); event.stopPropagation();" title="Remove from saved">remove</button>
    <button class="action" onclick="archiveThread('{html.escape(tid)}', this); event.stopPropagation();" title="Archive thread">archive</button>
  </span>
  <div class="messages">
    <div class="msg">
      <div class="msg__head"><span class="msg__sender">{html.escape(entry.get('from',''))}</span></div>
      <div class="msg__subject">{html.escape(entry.get('subject',''))}</div>
      <div class="msg__snippet">{html.escape(entry.get('snippet',''))}</div>
    </div>
  </div>
</div>
""")
    return f"""
<section class="bucket" id="bsaved">
  <div class="bucket__head bucket--reading">
    <span class="bucket__title">Saved to read later</span>
    <span class="bucket__count">{len(saved)} threads</span>
  </div>
  {"".join(rows)}
</section>
"""


def render_bucket(
    bucket_id: str,
    threads: dict[str, list[dict[str, Any]]],
) -> str:
    meta = BUCKETS.get(bucket_id, {"name": "Unknown", "annot": "ambig"})
    annot = meta["annot"]
    n_threads = len(threads)
    n_msgs = sum(len(v) for v in threads.values())

    if not threads:
        body = '<div class="bucket__empty">Empty.</div>'
        bulk = ""
    else:
        sorted_threads = sorted(
            threads.items(),
            key=lambda kv: max(parse_date(m.get("date", "")) for m in kv[1]),
            reverse=True,
        )
        body = "\n".join(
            render_thread(tid, msgs, bucket_id)
            for tid, msgs in sorted_threads
        )
        bulk_kind = meta.get("bulk")
        if bulk_kind == "smart":
            bulk = f'<button class="bucket__bulk" onclick="bulkBucket(\'{bucket_id}\', true, this)">smart all</button>'
        elif bulk_kind == "archive":
            bulk = f'<button class="bucket__bulk" onclick="bulkBucket(\'{bucket_id}\', false, this)">archive all</button>'
        else:
            bulk = ""

    count_label = f"{n_threads} threads / {n_msgs} msgs" if n_msgs != n_threads else f"{n_threads} threads"
    return f"""
<section class="bucket" id="b{bucket_id}">
  <div class="bucket__head bucket--{annot}">
    <span class="bucket__title">{html.escape(meta['name'])}</span>
    <span class="bucket__count">{count_label}</span>
    {bulk}
  </div>
  {body}
</section>
"""


PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Email digest</title>
<style>{css}</style>
</head><body>
<div class="shell">
  <header class="masthead">
    <div>
      <span class="masthead__brand">Email digest</span>
      <span class="masthead__date">{today}</span>
    </div>
    <nav class="masthead__nav">
      <button class="nav-btn" onclick="refreshDigest(this)" title="Re-pull the latest 100 inbox messages and re-classify them">↻ Refresh</button>
      <button class="nav-btn" onclick="categorizeDigest(this)" title="Run the LLM-judge self-review pass over reply-needed and FYI threads, applying the SKILL.md categorization rules">⚖ Categorize</button>
      <button class="nav-btn" onclick="refreshAndCategorize(this)" title="Refresh + Categorize back-to-back; the inbox stays visible and the judge's moves animate into place">Refresh & Categorize</button>
      <a href="{prefix}/settings">Settings & rules →</a>
    </nav>
  </header>

  <div class="summary">
    <span><b id="sum-msgs">{total_msgs}</b> messages</span>
    <span><b id="sum-threads">{total_threads}</b> threads</span>
    <span><b id="sum-keep">{keep}</b> to keep</span>
    <span><b id="sum-archive">{archive}</b> to archive</span>
    <span id="jump-mount">{jump}</span>
  </div>

  <div id="bucket-mount">{bucket_html}</div>
</div>
<div id="toast-region"></div>
<script>{js}</script>
</body></html>
"""


def _bucket_view(data: dict[str, Any]) -> dict[str, Any]:
    """Build the bucket-section HTML + summary numbers from a data.json dict.

    Shared by the full page (`index`) and the `/api/buckets` fragment endpoint
    so both render identically — the latter lets the client re-render the inbox
    in place (and FLIP-animate the judge's moves) without a full page reload.
    """
    msgs = data.get("messages", [])

    # Apply manual per-thread moves on top of the classifier's buckets, so a
    # "move to" decision sticks across refreshes.
    apply_bucket_overrides(msgs, load_bucket_overrides())

    # Starred threads are pulled OUT of their natural bucket and shown only
    # in the Starred section at the top, per the manual-override semantics.
    starred_ids = {e.get("thread_id") for e in load_starred_threads()}

    by_bucket: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for m in msgs:
        tid = m.get("threadId", m["id"])
        if tid in starred_ids:
            continue
        bucket = m.get("final_bucket", "?")
        by_bucket[bucket][tid].append(m)

    order = ["1", "2", "4", "5", "6", "3", "10", "9", "7", "8", "?"]
    # Starred section is at the very top. Mount div lets the JS refresh the
    # section after star/unstar without a full reload.
    parts = [f'<div id="starred-mount">{render_starred_section()}</div>']
    for b in order:
        parts.append(render_bucket(b, by_bucket.get(b, {})))
    bucket_html = "\n".join(parts)
    bucket_html += f'<div id="saved-mount">{render_saved_section()}</div>'

    jump_parts = []
    if starred_ids:
        jump_parts.append(
            f'<a href="#bstarred">★ Starred<span style="color:var(--ink-faint);"> · {len(starred_ids)}</span></a>'
        )
    for b in order:
        n = sum(len(v) for v in by_bucket.get(b, {}).values())
        if n > 0:
            jump_parts.append(
                f'<a href="#b{b}">{html.escape(BUCKETS.get(b, {}).get("name","?"))}<span style="color:var(--ink-faint);"> · {n}</span></a>'
            )
    jump = " ".join(jump_parts)

    archive_buckets = {"7", "8", "9", "10"}
    keep_count = sum(sum(len(v) for v in by_bucket.get(k, {}).values()) for k in by_bucket if k not in archive_buckets)
    archive_count = sum(sum(len(v) for v in by_bucket.get(k, {}).values()) for k in by_bucket if k in archive_buckets)
    total_threads = sum(len(by_bucket[b]) for b in by_bucket)

    return {
        "bucket_html": bucket_html,
        "jump": jump,
        "total_msgs": len(msgs),
        "total_threads": total_threads,
        "keep": keep_count,
        "archive": archive_count,
    }


def load_digest() -> dict[str, Any]:
    """The classifier's latest output, or an empty digest when it has never run.

    A freshly adopted workspace has no data.json until the email-digest skill
    runs for the first time, and the page has to render before then -- so a
    missing file is the normal empty state, not an error. ``_bucket_view``
    reads ``messages`` with a default, so an empty dict renders zero buckets.
    """
    if not DATA_PATH.exists():
        return {}
    return json.loads(DATA_PATH.read_text())


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    v = _bucket_view(load_digest())
    return HTMLResponse(PAGE.format(
        css=CSS, js=JS, prefix=ROOT_PATH,
        today=datetime.now().strftime("%a %d %b %Y"),
        total_msgs=v["total_msgs"], total_threads=v["total_threads"],
        keep=v["keep"], archive=v["archive"], jump=v["jump"], bucket_html=v["bucket_html"],
    ))


@app.get("/api/buckets")
def api_buckets() -> JSONResponse:
    """Re-render the bucket section + summary numbers from the current
    data.json so the client can refresh the inbox in place (and FLIP-animate
    the judge's re-bucketing) without a full page reload."""
    return JSONResponse(_bucket_view(load_digest()))


# ---- Action endpoints ----

@app.get("/api/message/{message_id}")
def api_message_body(message_id: str) -> JSONResponse:
    """Lazy-load one message's full body from Gmail on demand (when a thread is
    expanded). The refresh pipeline only stores the short snippet, so this
    fetches ``format=full`` and decodes the body just for the opened message."""
    data = gmail_actions._gmail_call(
        "GET", f"/gmail/v1/users/me/messages/{message_id}?format=full"
    )
    payload = data.get("payload")
    if not payload:
        detail = (data.get("error") or {}).get("message") or "message not found"
        raise HTTPException(404, detail)
    html_body, mime = extract_message_html(payload)
    return JSONResponse({"id": message_id, "html": html_body, "mime": mime})


@app.post("/api/move")
async def api_move(req: Request) -> JSONResponse:
    """Record a manual bucket move for a thread. Durable: applied on top of the
    classifier on every render, so it survives a Refresh."""
    body = await req.json()
    thread_id = body.get("thread_id")
    bucket = body.get("bucket")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    if bucket not in BUCKETS:
        raise HTTPException(400, f"unknown bucket {bucket!r}")
    overrides = load_bucket_overrides()
    # Log the move as labeled training data BEFORE updating the override, so we
    # capture where it was (override-or-classifier) just before this move.
    try:
        data = json.loads(DATA_PATH.read_text())
        msgs = [m for m in data.get("messages", []) if m.get("threadId", m.get("id")) == thread_id]
    except (FileNotFoundError, json.JSONDecodeError):
        msgs = []
    from_bucket = overrides.get(thread_id) or (msgs[0].get("final_bucket", "") if msgs else "")
    record = build_move_record(thread_id, bucket, from_bucket, msgs, datetime.now(timezone.utc).isoformat())
    MOVE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MOVE_LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    overrides[thread_id] = bucket
    save_bucket_overrides(overrides)
    return JSONResponse({"ok": True, "thread_id": thread_id, "bucket": bucket, "name": BUCKETS[bucket]["name"]})


@app.post("/api/archive")
async def api_archive(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    return JSONResponse(gmail_actions.archive(thread_id))


@app.post("/api/smart-action")
async def api_smart_action(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    bucket = body.get("bucket", "")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    data = json.loads(DATA_PATH.read_text())
    msgs = [m for m in data.get("messages", []) if m.get("threadId") == thread_id]
    if not msgs:
        return JSONResponse(gmail_actions.archive(thread_id))
    msg = msgs[0]
    return JSONResponse(gmail_actions.smart_action(
        message_id=msg["id"],
        thread_id=thread_id,
        bucket=bucket,
        snippet=msg.get("snippet", ""),
        sender=msg.get("from", ""),
        subject=msg.get("subject", ""),
    ))


# Refresh runs ~60-80s end-to-end (propagate + classify + LLM judge), which
# is longer than the workspace_server's 30s httpx timeout. Kick the work off
# in a background thread and let the client poll for completion instead of
# holding a single HTTP request open the whole time.
_refresh_state: dict[str, Any] = {
    "in_progress": False,
    "started_at": None,
    "finished_at": None,
    "step": None,
    "result": None,
    "error": None,
}


def _do_refresh(n: int) -> None:
    """Inbox-pull pipeline: propagate mutes, classify, synthesize.
    Does NOT run the LLM judge — that's a separate /api/categorize step."""
    try:
        _refresh_state["step"] = "propagate_mutes"
        propagate = subprocess.run(
            ["uv", "run", "python", ".agents/skills/email-digest/scripts/propagate_mutes.py", str(n)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
            timeout=120,
        )
        propagate_msg = (
            propagate.stderr.strip().splitlines()[-1]
            if propagate.stderr.strip() else "propagate skipped"
        )

        _refresh_state["step"] = "classify"
        classify = subprocess.run(
            ["uv", "run", ".agents/skills/email-digest/scripts/classify.py", str(n)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
        if classify.returncode != 0:
            _refresh_state["error"] = f"classify failed: {classify.stderr[-500:]}"
            return

        _refresh_state["step"] = "synthesize"
        synth = subprocess.run(
            ["uv", "run", "python", ".agents/skills/email-digest/scripts/synthesize_overrides.py"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
        if synth.returncode != 0:
            _refresh_state["error"] = f"synthesize failed: {synth.stderr[-500:]}"
            return

        data = json.loads(DATA_PATH.read_text())
        _refresh_state["result"] = {
            "messages": len(data.get("messages", [])),
            "elapsed_sec": data.get("stats", {}).get("elapsed_sec"),
            "propagate_mutes": propagate_msg,
        }
    except Exception as e:  # noqa: BLE001 — surface the message to the user
        _refresh_state["error"] = f"refresh crashed: {e!r}"
    finally:
        _refresh_state["in_progress"] = False
        _refresh_state["finished_at"] = time.time()
        _refresh_state["step"] = None


def _do_categorize() -> None:
    """LLM-judge self-review pass only. Runs in a background thread; client
    polls /api/categorize-status. Assumes classify.py has already populated
    data.json — typically run after Refresh."""
    try:
        _categorize_state["step"] = "llm_judge"
        judge = subprocess.run(
            ["uv", "run", "python", ".agents/skills/email-digest/scripts/llm_judge.py"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
            timeout=240,
        )
        if judge.returncode != 0:
            _categorize_state["error"] = f"llm_judge failed: {judge.stderr[-300:]}"
            return
        data = json.loads(DATA_PATH.read_text())
        _categorize_state["result"] = {
            "messages": len(data.get("messages", [])),
            "llm_judge_moves": data.get("stats", {}).get("llm_judge_moves", 0),
        }
    except Exception as e:  # noqa: BLE001
        _categorize_state["error"] = f"categorize crashed: {e!r}"
    finally:
        _categorize_state["in_progress"] = False
        _categorize_state["finished_at"] = time.time()
        _categorize_state["step"] = None


_categorize_state: dict[str, Any] = {
    "in_progress": False,
    "started_at": None,
    "finished_at": None,
    "step": None,
    "result": None,
    "error": None,
}


@app.post("/api/refresh")
def api_refresh(n: int = 200) -> JSONResponse:
    """Kick off the inbox-pull pipeline (propagate + classify + synthesize)
    in a background thread; client polls `/api/refresh-status` for progress.
    The LLM judge is a separate endpoint (/api/categorize)."""
    if _refresh_state["in_progress"]:
        return JSONResponse({
            "ok": True,
            "already_running": True,
            "started_at": _refresh_state["started_at"],
            "step": _refresh_state["step"],
        })
    _refresh_state["in_progress"] = True
    _refresh_state["started_at"] = time.time()
    _refresh_state["finished_at"] = None
    _refresh_state["step"] = "queued"
    _refresh_state["result"] = None
    _refresh_state["error"] = None
    threading.Thread(target=_do_refresh, args=(n,), daemon=True).start()
    return JSONResponse({"ok": True, "started": True})


@app.get("/api/refresh-status")
def api_refresh_status() -> JSONResponse:
    return JSONResponse({
        "in_progress": _refresh_state["in_progress"],
        "step": _refresh_state["step"],
        "started_at": _refresh_state["started_at"],
        "finished_at": _refresh_state["finished_at"],
        "result": _refresh_state["result"],
        "error": _refresh_state["error"],
    })


@app.post("/api/categorize")
def api_categorize() -> JSONResponse:
    """Kick off the LLM-judge self-review pass in a background thread.
    Operates on the data.json already produced by /api/refresh."""
    if _categorize_state["in_progress"]:
        return JSONResponse({
            "ok": True,
            "already_running": True,
            "started_at": _categorize_state["started_at"],
            "step": _categorize_state["step"],
        })
    _categorize_state["in_progress"] = True
    _categorize_state["started_at"] = time.time()
    _categorize_state["finished_at"] = None
    _categorize_state["step"] = "queued"
    _categorize_state["result"] = None
    _categorize_state["error"] = None
    threading.Thread(target=_do_categorize, daemon=True).start()
    return JSONResponse({"ok": True, "started": True})


@app.get("/api/categorize-status")
def api_categorize_status() -> JSONResponse:
    return JSONResponse({
        "in_progress": _categorize_state["in_progress"],
        "step": _categorize_state["step"],
        "started_at": _categorize_state["started_at"],
        "finished_at": _categorize_state["finished_at"],
        "result": _categorize_state["result"],
        "error": _categorize_state["error"],
    })


@app.get("/api/saved-section", response_class=HTMLResponse)
def api_saved_section() -> HTMLResponse:
    """Return just the rendered Saved-section HTML. Client refreshes this
    after save/unsave so the section appears/updates without a full reload."""
    return HTMLResponse(render_saved_section())


@app.post("/api/save-thread")
async def api_save_thread(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    # Look up the thread in our snapshot to capture sender/subject/snippet/synthesis.
    data = json.loads(DATA_PATH.read_text())
    msgs = [m for m in data.get("messages", []) if m.get("threadId") == thread_id]
    if not msgs:
        raise HTTPException(404, "thread not in current snapshot")
    msgs.sort(key=lambda m: m.get("date", ""), reverse=True)
    most_recent = msgs[0]
    save_thread_entry({
        "thread_id": thread_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "from": most_recent.get("from", ""),
        "subject": most_recent.get("subject", ""),
        "snippet": most_recent.get("snippet", ""),
        "synthesis": most_recent.get("synthesis", ""),
        "n_messages": len(msgs),
    })
    return JSONResponse({"ok": True, "thread_id": thread_id})


@app.post("/api/unsave-thread")
async def api_unsave_thread(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    unsave_thread_entry(thread_id)
    return JSONResponse({"ok": True, "thread_id": thread_id})


@app.get("/api/starred-section", response_class=HTMLResponse)
def api_starred_section() -> HTMLResponse:
    """Return the rendered Starred-section HTML. Client refreshes this after
    star/unstar so the section appears/updates without a full reload."""
    return HTMLResponse(render_starred_section())


@app.post("/api/star-thread")
async def api_star_thread(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    data = json.loads(DATA_PATH.read_text())
    msgs = [m for m in data.get("messages", []) if m.get("threadId") == thread_id]
    if not msgs:
        raise HTTPException(404, "thread not in current snapshot")
    msgs.sort(key=lambda m: m.get("date", ""), reverse=True)
    most_recent = msgs[0]
    # Capture the natural bucket so unstar knows where to send it back.
    # The actual rendering on unstar just relies on data.json staying intact,
    # but storing it here gives us a fallback display label.
    star_thread_entry({
        "thread_id": thread_id,
        "starred_at": datetime.now(timezone.utc).isoformat(),
        "from": most_recent.get("from", ""),
        "subject": most_recent.get("subject", ""),
        "snippet": most_recent.get("snippet", ""),
        "synthesis": most_recent.get("synthesis", ""),
        "natural_bucket": most_recent.get("final_bucket", "?"),
        "n_messages": len(msgs),
    })
    return JSONResponse({"ok": True, "thread_id": thread_id})


@app.post("/api/unstar-thread")
async def api_unstar_thread(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    unstar_thread_entry(thread_id)
    return JSONResponse({"ok": True, "thread_id": thread_id})


@app.post("/api/undo")
async def api_undo(req: Request) -> JSONResponse:
    body = await req.json()
    thread_id = body.get("thread_id")
    undo_add = body.get("undo_add") or ["INBOX"]
    undo_remove = body.get("undo_remove") or []
    if not thread_id:
        raise HTTPException(400, "thread_id required")
    return JSONResponse(gmail_actions.undo_action(thread_id, undo_add, undo_remove))


# ---- Settings page ----

SETTINGS_CSS = CSS + """
.file-editor { margin-bottom: 24px; }
.file-editor__head {
    display: flex; align-items: baseline; gap: 8px;
    padding: 4px 0; margin-bottom: 6px;
    border-bottom: 1px solid var(--rule);
}
.file-editor__name { font-size: 13px; font-weight: 600; color: var(--ink); }
.file-editor__path { font-family: var(--font-mono); font-size: 11px; color: var(--ink-faint); }
.file-editor__status {
    margin-left: auto; font-size: 11px; color: var(--ink-faint);
}
.file-editor__status.saving { color: var(--accent-warn); }
.file-editor__status.saved { color: var(--accent-done); }

/* Rendered (read-only) markdown view */
.rendered {
    padding: 10px 14px; border: 1px solid var(--rule-soft);
    border-radius: 4px; background: var(--panel);
    cursor: text; transition: background 120ms ease, border-color 120ms ease;
    font-size: 13px; line-height: 1.5; color: var(--ink);
}
.rendered:hover { background: var(--bg-hover); border-color: var(--rule); }
.rendered:empty::before { content: "(click to add content)"; color: var(--ink-faint); font-style: italic; }
.rendered h1 { font-size: 16px; font-weight: 600; margin: 8px 0 4px; }
.rendered h2 { font-size: 14px; font-weight: 600; margin: 12px 0 4px; color: var(--ink); }
.rendered h3 { font-size: 13px; font-weight: 600; margin: 8px 0 2px; color: var(--ink-soft); }
.rendered h4 { font-size: 11px; font-weight: 600; margin: 6px 0 2px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.04em; }
.rendered p { margin: 4px 0; }
.rendered ul, .rendered ol { margin: 4px 0; padding-left: 22px; }
.rendered li { margin: 1px 0; }
.rendered code {
    font-family: var(--font-mono); font-size: 11px;
    background: var(--bg-hover); padding: 1px 4px; border-radius: 2px;
}
.rendered pre {
    font-family: var(--font-mono); font-size: 11px; line-height: 1.45;
    background: var(--bg); border: 1px solid var(--rule-soft);
    padding: 8px 10px; border-radius: 3px; overflow-x: auto;
}
.rendered pre code { background: none; padding: 0; font-size: inherit; }
.rendered blockquote {
    margin: 6px 0; padding: 2px 12px;
    border-left: 2px solid var(--rule); color: var(--ink-soft);
}
.rendered hr { border: none; border-top: 1px solid var(--rule-soft); margin: 10px 0; }
.rendered table { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
.rendered th, .rendered td { border-bottom: 1px solid var(--rule-soft); padding: 3px 12px 3px 0; text-align: left; vertical-align: top; }
.rendered th { font-weight: 600; color: var(--ink-soft); }
.rendered a { color: var(--accent); border-bottom: 1px dotted var(--accent); text-decoration: none; }
.rendered strong { font-weight: 600; }
.rendered--plain {
    font-family: var(--font-mono); font-size: 11px; line-height: 1.5;
    white-space: pre-wrap; color: var(--ink);
}

/* Hidden editor that swaps in on click. Height is set in JS to match the
   rendered preview so the user lands where they expected, with no
   internal scrollbar. */
.file-editor textarea {
    display: none; width: 100%; padding: 10px 14px;
    font-family: var(--font-mono); font-size: 12px; line-height: 1.5;
    background: var(--panel); border: 1px solid var(--accent);
    border-radius: 4px; color: var(--ink); resize: vertical;
    overflow: hidden;
}
.file-editor.editing .rendered { display: none; }
.file-editor.editing textarea { display: block; }
.file-editor textarea:focus { outline: none; }
"""

SETTINGS_JS = r"""
const PREFIX = window.location.pathname.replace(/\/settings\/?$/, '');

async function saveFile(path, content, statusEl) {
    statusEl.textContent = 'saving'; statusEl.className = 'file-editor__status saving';
    try {
        const r = await fetch(PREFIX + '/api/files', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path, content}),
        });
        if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
        statusEl.textContent = 'saved'; statusEl.className = 'file-editor__status saved';
        setTimeout(() => { statusEl.textContent = ''; statusEl.className = 'file-editor__status'; }, 2000);
    } catch (e) {
        statusEl.textContent = 'save failed: ' + e.message;
        statusEl.className = 'file-editor__status';
    }
}

function renderMarkdown(text, isPlain) {
    if (isPlain) {
        const pre = document.createElement('div');
        pre.className = 'rendered--plain';
        pre.textContent = text;
        return pre.outerHTML;
    }
    return marked.parse(text);
}

document.addEventListener('DOMContentLoaded', () => {
    if (typeof marked !== 'undefined') {
        marked.setOptions({gfm: true, breaks: false, mangle: false, headerIds: false});
    }
    for (const editor of document.querySelectorAll('.file-editor')) {
        const ta = editor.querySelector('textarea');
        const rendered = editor.querySelector('.rendered');
        const status = editor.querySelector('.file-editor__status');
        const isPlain = editor.dataset.kind === 'plain';
        let last = ta.value;

        // Initial render of the markdown (or plain) view
        rendered.innerHTML = renderMarkdown(ta.value, isPlain);

        function fitTextarea() {
            // Auto-size the textarea to fit its content so there's no
            // internal scrollbar. Match (or exceed) the rendered preview's
            // height so the cursor lands where the user expected.
            ta.style.height = 'auto';
            const fit = Math.max(ta.scrollHeight + 4, rendered.offsetHeight || 0, 240);
            ta.style.height = fit + 'px';
        }

        rendered.addEventListener('click', () => {
            editor.classList.add('editing');
            fitTextarea();
            ta.focus();
            // Position cursor at end on first focus
            ta.selectionStart = ta.selectionEnd = ta.value.length;
        });

        // Grow as you type so the box matches content.
        ta.addEventListener('input', fitTextarea);

        ta.addEventListener('blur', async () => {
            editor.classList.remove('editing');
            if (ta.value !== last) {
                last = ta.value;
                await saveFile(ta.dataset.path, ta.value, status);
            }
            rendered.innerHTML = renderMarkdown(ta.value, isPlain);
        });
    }
});
"""

SETTINGS_PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Settings & rules</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>{css}</style>
</head><body>
<div class="shell">
  <header class="masthead">
    <div>
      <span class="masthead__brand">Settings & rules</span>
      <span class="masthead__date">rules · contacts · memory</span>
    </div>
    <nav class="masthead__nav">
      <a href="{prefix}/">← Back to digest</a>
    </nav>
  </header>
  <div class="summary"><span>Click any file to edit. Click out to save — no reload needed.</span></div>
  {editors}
</div>
<script>{js}</script>
</body></html>
"""


@app.get("/settings", response_class=HTMLResponse)
def settings() -> HTMLResponse:
    editors = []
    for rel in EDITABLE_FILES:
        path = REPO_ROOT / rel
        try:
            content = path.read_text()
        except FileNotFoundError:
            content = ""
        # .md files render as markdown; everything else (e.g. contacts.txt) as plain monospace
        kind = "plain" if not rel.endswith(".md") else "markdown"
        editors.append(f"""
<div class="file-editor" data-kind="{kind}">
  <div class="file-editor__head">
    <span class="file-editor__name">{html.escape(path.name)}</span>
    <span class="file-editor__path">{html.escape(rel)}</span>
    <span class="file-editor__status"></span>
  </div>
  <div class="rendered" title="Click to edit"></div>
  <textarea data-path="{html.escape(rel)}" spellcheck="false">{html.escape(content)}</textarea>
</div>
""")
    return HTMLResponse(SETTINGS_PAGE.format(
        css=SETTINGS_CSS, js=SETTINGS_JS, prefix=ROOT_PATH, editors="\n".join(editors),
    ))


@app.put("/api/files")
async def api_save_file(req: Request) -> JSONResponse:
    body = await req.json()
    rel = body.get("path", "")
    content = body.get("content", "")
    if rel not in EDITABLE_FILES:
        raise HTTPException(403, "path not in EDITABLE_FILES allowlist")
    path = REPO_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return JSONResponse({"ok": True, "path": rel, "bytes": len(content)})


@app.get("/api/files")
def api_list_files() -> JSONResponse:
    return JSONResponse({"files": EDITABLE_FILES})


@app.get("/api/files/{name:path}", response_class=PlainTextResponse)
def api_get_file(name: str) -> PlainTextResponse:
    if name not in EDITABLE_FILES:
        raise HTTPException(403, "path not in EDITABLE_FILES allowlist")
    return PlainTextResponse((REPO_ROOT / name).read_text())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8091)


if __name__ == "__main__":
    main()
