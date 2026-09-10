#!/usr/bin/env python3
"""Propagate the app's mute across thread follow-ups.

Background: when you (or the email-review smart-action) mute a thread, Gmail
applies the app's `Muted` label and removes the INBOX label. But Gmail does
NOT auto-propagate that to *future* messages on the same thread — when a
cold-outreach sender follows up days later, the new message arrives with a
fresh INBOX label and shows up in the digest again.

The fix: before each digest refresh, look at threads represented in the
current inbox (bounded by the digest's N-message window). For each, check
whether any message in the thread has the muted label. If yes, archive
the inbox-still-labeled messages so they never reach the digest.

This bounds the work to ~N messages (default 100) instead of every muted
thread in the mailbox — which would be too slow on a long-lived account.

Usage:
    uv run python .agents/skills/email-digest/scripts/propagate_mutes.py [N]

Idempotent. Safe to run before every refresh.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from typing import Any

from email_review.gmail_actions import get_muted_label_id


def gmail(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    args = ["latchkey", "curl", "-s", "-X", method, url]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else {}


def list_inbox_message_ids(n: int) -> list[str]:
    """Return up to n most recent inbox message IDs, paginated."""
    ids: list[str] = []
    page = None
    while len(ids) < n:
        url = (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            f"?q=in%3Ainbox&maxResults={min(500, n - len(ids))}"
        )
        if page:
            url += f"&pageToken={page}"
        res = gmail("GET", url)
        ids.extend(m["id"] for m in res.get("messages", []))
        page = res.get("nextPageToken")
        if not page:
            break
    return ids[:n]


def fetch_thread_ids(message_ids: list[str]) -> set[str]:
    """Return the unique threadIds for the given message IDs (parallel)."""
    def get_tid(mid: str) -> str:
        d = gmail(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=minimal",
        )
        return d.get("threadId", "")

    tids: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for tid in ex.map(get_tid, message_ids):
            if tid:
                tids.add(tid)
    return tids


def check_thread(thread_id: str, muted_label_id: str) -> tuple[str, bool, list[str]]:
    """Return (thread_id, has_muted_sibling, inbox_message_ids).

    If any message in this thread has the muted label, returns the inbox-
    labeled message IDs that need to be archived for the mute to propagate.
    """
    thread = gmail(
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}?format=minimal",
    )
    msgs = thread.get("messages", [])
    has_muted = any(muted_label_id in m.get("labelIds", []) for m in msgs)
    if not has_muted:
        return thread_id, False, []
    inbox_msgs = [m["id"] for m in msgs if "INBOX" in m.get("labelIds", [])]
    return thread_id, True, inbox_msgs


def archive_messages(message_ids: list[str]) -> None:
    if not message_ids:
        return
    for i in range(0, len(message_ids), 1000):
        batch = message_ids[i : i + 1000]
        gmail(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            {"ids": batch, "removeLabelIds": ["INBOX"]},
        )


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100

    muted_label_id = get_muted_label_id()

    print(f"Scanning {n} most recent inbox messages for muted-thread follow-ups…",
          file=sys.stderr)
    inbox_ids = list_inbox_message_ids(n)
    print(f"  pulled {len(inbox_ids)} inbox messages", file=sys.stderr)

    thread_ids = fetch_thread_ids(inbox_ids)
    print(f"  → {len(thread_ids)} unique threads to check", file=sys.stderr)

    to_archive: list[str] = []
    affected_threads = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for _, is_muted, inbox_msgs in ex.map(
            lambda tid: check_thread(tid, muted_label_id), thread_ids
        ):
            if is_muted and inbox_msgs:
                to_archive.extend(inbox_msgs)
                affected_threads += 1

    if to_archive:
        archive_messages(to_archive)
    print(
        f"Propagated mute to {len(to_archive)} follow-up message(s) "
        f"across {affected_threads} thread(s).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
