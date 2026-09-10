"""Preflight check before acting on a message's List-Unsubscribe header.

An unsubscribe that follows the *latest message's `List-Unsubscribe` header*
is dangerous when the mail reached you through one of your own internal Google
Groups / forwarders (e.g. a vendor sends to `ap@yourcompany.example`, which
forwards to you): that header points back at your own group, so "unsubscribing"
would remove you from your own forwarder rather than from the real sender. This
script inspects the header and refuses that case, reusing the exact same domain
check the web-service smart-action path uses (`_is_internal_forwarder_unsub`).

Usage:
    uv run python .agents/skills/email-digest/scripts/check_unsub_target.py \
        --thread-id 19b6b87d76956f8a
    uv run python .agents/skills/email-digest/scripts/check_unsub_target.py \
        --message-id 19b6b87d76956f8a

Exit codes:
    0  external List-Unsubscribe header present -> SAFE to unsubscribe
    2  header targets one of your own internal-forwarder domains -> DO NOT
       unsubscribe; find the sender's real opt-out link in the body instead
    3  no List-Unsubscribe header at all -> nothing to act on; look for the
       body opt-out link
"""

from __future__ import annotations

import argparse
import json
import sys

# Reuse the canonical helpers from the web-service path so the domain list and
# header parsing stay in one place (no re-implementation).
from email_review.gmail_actions import (
    _gmail_call,
    _is_internal_forwarder_unsub,
    get_message_headers,
)


def _latest_message_id_in_thread(thread_id: str) -> str:
    """Return the id of the most recent message in a thread (the one a
    header-based unsubscribe would act on)."""
    thread = _gmail_call("GET", f"/gmail/v1/users/me/threads/{thread_id}?format=minimal")
    messages = thread.get("messages", [])
    if not messages:
        raise SystemExit(f"thread {thread_id} has no messages (or could not be fetched)")
    return messages[-1]["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--thread-id", help="Gmail thread id (resolves to the latest message)")
    group.add_argument("--message-id", help="Gmail message id (checked directly)")
    args = ap.parse_args()

    message_id = args.message_id or _latest_message_id_in_thread(args.thread_id)
    headers = get_message_headers(message_id)
    list_unsub = headers.get("list-unsubscribe", "")

    if not list_unsub:
        verdict = "no-header"
        exit_code = 3
    elif _is_internal_forwarder_unsub(list_unsub):
        verdict = "internal-forwarder"
        exit_code = 2
    else:
        verdict = "external"
        exit_code = 0

    print(
        json.dumps(
            {
                "message_id": message_id,
                "list_unsubscribe": list_unsub,
                "verdict": verdict,
                "safe_to_unsubscribe": exit_code == 0,
            },
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
