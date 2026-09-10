"""Gmail action helpers — archive/spam/mute/unsubscribe via latchkey.

These are sync, called from FastAPI route handlers. They all return the set
of labels removed/added so the undo endpoint can reverse the change.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from email_review.account import ACCOUNT_ADDRS, ORG_DOMAINS

# Gmail's REST API doesn't expose the web-UI "mute" action, so the app manages
# its own plain Gmail label (named below) and applies it to muted threads. The
# label id is resolved (and created on first use) at runtime via the labels
# API — see get_muted_label_id().
MUTED_LABEL_NAME = "Muted"
_muted_label_id: str | None = None

CONTACTS_PATH = Path(".agents/skills/email-digest/contacts.txt")

# Domains you are internal to (from account.ORG_DOMAINS). When a
# List-Unsubscribe URL or mailto target lives under one of these, treat the
# link as an internal forwarder / manage-subscription page (e.g. a company
# Google Group) and refuse the unsubscribe — clicking it would remove you from
# the forwarder entirely rather than from the real sender's list.
INTERNAL_FORWARDER_DOMAINS = tuple(ORG_DOMAINS)


# Phishing detection lives in email_review.phishing (shared with classify.py).
# Use the same function in both places so a detection-gap fix lands once.
from email_review.phishing import has_phishing_tells as _has_phishing_tells


def _looks_like_phishing(sender: str, subject: str, snippet: str = "") -> bool:
    """Smart-action's phishing check — delegates to the shared detector.
    Kept as a small wrapper so callers can pass just (sender, subject) when
    they don't have the snippet handy (the shared function accepts both)."""
    return _has_phishing_tells(subject or "", sender or "", snippet or "", None)


def _is_internal_forwarder_unsub(list_unsub_header: str) -> bool:
    """Return True if the List-Unsubscribe header points back at one of your
    own domains (i.e. it's an internal Google Group / forwarder, not a real
    third-party mailing list)."""
    if not list_unsub_header:
        return False
    lower = list_unsub_header.lower()
    return any(d in lower for d in INTERNAL_FORWARDER_DOMAINS)


def _load_keep_subscribed() -> set[str]:
    """Read contacts.txt and return the keep-subscribed entries. These are
    senders the user wants to stay subscribed to — smart-action archives
    them but must NEVER unsubscribe.
    """
    out: set[str] = set()
    if not CONTACTS_PATH.exists():
        return out
    for line in CONTACTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[2].strip() == "keep-subscribed":
            out.add(parts[0].strip().lower())
    return out


def _is_keep_subscribed(addr_or_full_from: str) -> bool:
    """Match either a full email (foo@bar.com) or bare domain (bar.com)."""
    keep = _load_keep_subscribed()  # reload each call so contacts.txt edits take effect immediately
    if not keep or not addr_or_full_from:
        return False
    m = re.search(r"<([^>]+)>", addr_or_full_from)
    addr = (m.group(1) if m else addr_or_full_from).lower()
    if addr in keep:
        return True
    d = addr.split("@", 1)[1] if "@" in addr else ""
    return d in keep


class GatewayUnreachable(RuntimeError):
    """The latchkey gateway (Gmail credential proxy) can't be reached. Raised so
    callers can return a clear 'Gmail unavailable' message instead of a raw 500."""


def _gmail_call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke the Gmail API via latchkey curl. Returns parsed JSON.

    On a curl/gateway failure, raises GatewayUnreachable with a clear message
    rather than a bare CalledProcessError → 500, so callers can surface a
    'Gmail unavailable' message and the user can retry.
    """
    args = ["latchkey", "curl", "-s", "-X", method, f"https://gmail.googleapis.com{path}"]
    if body is not None:
        args.extend(["-H", "Content-Type: application/json", "-d", json.dumps(body)])
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GatewayUnreachable(
            "Gmail is unreachable — the latchkey gateway (credential proxy) appears to be down. "
            "This is a workspace/host connection issue, not the digest; try again once it's restored."
        )
    out = proc.stdout
    if not out.strip():
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out[:200]}


def get_muted_label_id() -> str:
    """Return the Gmail label id for the app's "Muted" label, creating the
    label on first use. Cached for the process lifetime.

    Gmail has no API-visible mute action, so muting a thread is just applying
    this label (and removing INBOX). Resolving by name means the app works on a
    plain Gmail account with no external client.
    """
    global _muted_label_id
    if _muted_label_id is not None:
        return _muted_label_id
    listing = _gmail_call("GET", "/gmail/v1/users/me/labels")
    for label in listing.get("labels", []):
        if label.get("name") == MUTED_LABEL_NAME:
            _muted_label_id = label["id"]
            return _muted_label_id
    created = _gmail_call(
        "POST",
        "/gmail/v1/users/me/labels",
        {
            "name": MUTED_LABEL_NAME,
            "labelListVisibility": "labelHide",
            "messageListVisibility": "hide",
        },
    )
    label_id = created.get("id")
    if not label_id:
        raise GatewayUnreachable(
            f"Could not resolve or create the {MUTED_LABEL_NAME!r} Gmail label."
        )
    _muted_label_id = label_id
    return _muted_label_id


def modify_message(message_id: str, add: list[str], remove: list[str]) -> dict[str, Any]:
    return _gmail_call(
        "POST",
        f"/gmail/v1/users/me/messages/{message_id}/modify",
        {"addLabelIds": add, "removeLabelIds": remove},
    )


def modify_thread(thread_id: str, add: list[str], remove: list[str]) -> dict[str, Any]:
    return _gmail_call(
        "POST",
        f"/gmail/v1/users/me/threads/{thread_id}/modify",
        {"addLabelIds": add, "removeLabelIds": remove},
    )


def get_message_full(message_id: str) -> dict[str, Any]:
    return _gmail_call("GET", f"/gmail/v1/users/me/messages/{message_id}?format=full")


def get_message_headers(message_id: str) -> dict[str, str]:
    """Return a lowercase-keyed dict of message headers."""
    msg = _gmail_call(
        "GET",
        f"/gmail/v1/users/me/messages/{message_id}"
        "?format=metadata"
        "&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Subject"
        "&metadataHeaders=List-Unsubscribe&metadataHeaders=List-Unsubscribe-Post",
    )
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def archive(thread_id: str) -> dict[str, Any]:
    """Remove the INBOX label from a thread. Returns the label diff for undo."""
    modify_thread(thread_id, add=[], remove=["INBOX"])
    return {"action": "archived", "thread_id": thread_id, "added": [], "removed": ["INBOX"]}


def unarchive(thread_id: str) -> dict[str, Any]:
    modify_thread(thread_id, add=["INBOX"], remove=[])
    return {"action": "unarchived", "thread_id": thread_id}


def mark_spam(thread_id: str) -> dict[str, Any]:
    modify_thread(thread_id, add=["SPAM"], remove=["INBOX"])
    return {"action": "spam", "thread_id": thread_id, "added": ["SPAM"], "removed": ["INBOX"]}


def unspam(thread_id: str) -> dict[str, Any]:
    modify_thread(thread_id, add=["INBOX"], remove=["SPAM"])
    return {"action": "unspammed", "thread_id": thread_id}


def mute(thread_id: str) -> dict[str, Any]:
    """Apply the app's Muted label and remove the thread from INBOX."""
    label_id = get_muted_label_id()
    modify_thread(thread_id, add=[label_id], remove=["INBOX"])
    return {"action": "muted", "thread_id": thread_id, "added": [label_id], "removed": ["INBOX"]}


def unmute(thread_id: str) -> dict[str, Any]:
    label_id = get_muted_label_id()
    modify_thread(thread_id, add=["INBOX"], remove=[label_id])
    return {"action": "unmuted", "thread_id": thread_id}


def try_unsubscribe(message_id: str, thread_id: str) -> tuple[bool, str]:
    """Best-effort unsubscribe using the message's List-Unsubscribe header.

    Returns (succeeded, method_description). When the header is absent or
    the request fails, returns (False, reason) and the caller should fall
    back to mute or archive.
    """
    headers = get_message_headers(message_id)
    list_unsub = headers.get("list-unsubscribe", "")
    if not list_unsub:
        return False, "no List-Unsubscribe header"

    if _is_internal_forwarder_unsub(list_unsub):
        return False, (
            f"List-Unsubscribe targets your own domain "
            f"({list_unsub[:80]}) — refusing (internal forwarder)"
        )

    # Try HTTPS URL first. RFC8058 (List-Unsubscribe-Post) means we POST
    # `List-Unsubscribe=One-Click` to the URL. Otherwise GET it.
    http_match = re.search(r"<(https?://[^>]+)>", list_unsub)
    if http_match:
        url = http_match.group(1)
        try:
            if "List-Unsubscribe-Post" in headers or "list-unsubscribe-post" in headers:
                data = urllib.parse.urlencode({"List-Unsubscribe": "One-Click"}).encode()
                req = urllib.request.Request(url, data=data, method="POST")
            else:
                req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                ok = 200 <= resp.status < 400
            return ok, f"HTTP unsubscribe to {url}"
        except (urllib.error.URLError, TimeoutError) as e:
            return False, f"HTTP unsubscribe failed: {e}"

    # Mailto fallback
    mailto_match = re.search(r"<mailto:([^>]+)>", list_unsub)
    if mailto_match:
        target = mailto_match.group(1)
        # Build a minimal RFC 822 message and send via Gmail, from your own
        # account address (the adopter sets this in account.py).
        from_addr = next(iter(sorted(ACCOUNT_ADDRS)), "me")
        raw = (
            f"To: {target}\r\n"
            f"From: {from_addr}\r\n"
            f"Subject: unsubscribe\r\n"
            f"\r\n"
            f"unsubscribe\r\n"
        )
        encoded = base64.urlsafe_b64encode(raw.encode()).decode()
        _gmail_call(
            "POST",
            "/gmail/v1/users/me/messages/send",
            {"raw": encoded},
        )
        return True, f"Mailto unsubscribe sent to {target}"

    return False, "List-Unsubscribe header had no usable URL or mailto"


def smart_action(message_id: str, thread_id: str, bucket: str, snippet: str = "", sender: str = "", subject: str = "") -> dict[str, Any]:
    """Apply the smart-action ruleset:

    - **If a usable unsubscribe option exists, unsubscribe** -- for ANY bucket,
      including cold outreach (bucket 6). Uses the message's List-Unsubscribe
      header (one-click POST / GET / mailto).
    - When there is no usable List-Unsubscribe header, fall back to **mute**
      for the subscription-type and cold-outreach buckets (6/7/8/9) — muting
      applies the app's Muted label and suppresses follow-ups without
      confirming a live address — or **archive** otherwise.
    - Mark as spam if it looks like spam (suspicious sender / phishing tells).

    Three protective exceptions override "always unsubscribe" -- each would harm
    the user rather than just leave a mailing list:
      - keep-subscribed senders (contacts.txt) -> archive only;
      - internal forwarders (List-Unsubscribe on one of your own domains, e.g. a
        company Google Group) -> archive only (unsubscribing drops you from the
        group);
      - phishing (bucket 7 tells) -> mark spam (never click a phisher's link).

    **Every path archives** (removes the INBOX label). Mute, spam, and
    unsubscribe additionally apply their own label/flag, but the thread
    always leaves the inbox so the digest doesn't show it again. Encoded
    as an invariant — each `modify_thread` call below MUST include
    `remove=["INBOX"]` (or be followed by an archive call) regardless of
    the action.

    Returns a dict describing what happened so the toast can show it and
    the undo handler can reverse it.
    """
    # 0. Keep-subscribed override: if the sender is on the keep-subscribed
    # list (contacts.txt), never unsubscribe — just archive. The user
    # explicitly wants to stay on these mailing lists.
    if _is_keep_subscribed(sender):
        modify_thread(thread_id, add=[], remove=["INBOX"])
        return {
            "action": "archived",
            "thread_id": thread_id,
            "detail": "Sender is keep-subscribed (per contacts.txt) — archived only",
            "undo_add": ["INBOX"],
            "undo_remove": [],
        }

    # 0.5: Phishing/impersonation in bucket 7 → mark as spam (NOT unsubscribe).
    # The unsubscribe flow trusts the sender's list-unsubscribe header, which
    # is exactly what phishers exploit; marking as spam is the right action
    # because it both removes from inbox AND teaches Gmail's filter. Detect
    # via the same heuristics as classify.py:
    #   - Display name impersonates you but the From address isn't yours.
    #   - Subject matches a known phishing signature (INV/PO with random
    #     alphanumeric suffix, gift-card scams, signature-request spoofing).
    if bucket == "7" and _looks_like_phishing(sender, subject, snippet):
        modify_thread(thread_id, add=["SPAM"], remove=["INBOX"])
        return {
            "action": "spam",
            "thread_id": thread_id,
            "detail": "Phishing tells (display-name impersonation or signature-spam subject) — marked as spam",
            "undo_add": ["INBOX"],
            "undo_remove": ["SPAM"],
        }

    # 1. If a usable unsubscribe option exists, unsubscribe -- for ANY bucket
    # (including cold outreach, bucket 6). The protective exceptions are the
    # keep-subscribed check above (step 0), the phishing check above (step
    # 0.5), and the internal-forwarder check below.
    headers = get_message_headers(message_id)
    list_unsub = headers.get("list-unsubscribe", "")

    # Internal forwarder (List-Unsubscribe on one of your own domains, e.g. a
    # company Google Group): unsubscribing would drop you from the group.
    # Archive only -- never unsubscribe.
    if list_unsub and _is_internal_forwarder_unsub(list_unsub):
        modify_thread(thread_id, add=[], remove=["INBOX"])
        return {
            "action": "archived",
            "thread_id": thread_id,
            "detail": "List-Unsubscribe link is on your own domain — internal forwarder, archived only",
            "undo_add": ["INBOX"],
            "undo_remove": [],
        }

    # A real List-Unsubscribe option is present -> try to unsubscribe.
    if list_unsub:
        unsub_ok, unsub_reason = try_unsubscribe(message_id, thread_id)
        if unsub_ok:
            modify_thread(thread_id, add=[], remove=["INBOX"])
            return {
                "action": "unsubscribed",
                "thread_id": thread_id,
                "detail": unsub_reason,
                "undo_add": ["INBOX"],
                "undo_remove": [],
            }
        # Header present but the direct call failed -> fall through to the
        # mute/archive fallback below.

    # 2. No usable unsubscribe. For the subscription-type buckets (7 marketing,
    # 8 in-product, 9 reading) and cold outreach (bucket 6), mute the thread:
    # apply the Muted label and remove INBOX so follow-ups on the thread stop
    # reaching the digest, without confirming a live address to the sender.
    if bucket in {"6", "7", "8", "9"}:
        label_id = get_muted_label_id()
        modify_thread(thread_id, add=[label_id], remove=["INBOX"])
        return {
            "action": "muted",
            "thread_id": thread_id,
            "detail": "No usable unsubscribe option — muted (Muted label applied, removed from inbox)",
            "undo_add": ["INBOX"],
            "undo_remove": [label_id],
        }

    # 3. Spammy heuristic: phishing tells in snippet, or sender domain suspicious
    snip_low = (snippet or "").lower()
    spam_tells = ["unsubscribe.*ach", "process the attached invoice via ach", "wire transfer"]
    if any(re.search(p, snip_low) for p in spam_tells):
        modify_thread(thread_id, add=["SPAM"], remove=["INBOX"])
        return {
            "action": "spam",
            "thread_id": thread_id,
            "detail": "Phishing tells in snippet",
            "undo_add": ["INBOX"],
            "undo_remove": ["SPAM"],
        }

    # 4. Default: archive (no unsubscribe option found).
    modify_thread(thread_id, add=[], remove=["INBOX"])
    return {
        "action": "archived",
        "thread_id": thread_id,
        "detail": "No unsubscribe option; archived",
        "undo_add": ["INBOX"],
        "undo_remove": [],
    }


def undo_action(thread_id: str, undo_add: list[str], undo_remove: list[str]) -> dict[str, Any]:
    """Reverse a prior action by re-adding/removing the recorded labels."""
    modify_thread(thread_id, add=undo_add, remove=undo_remove)
    return {"action": "undone", "thread_id": thread_id}
