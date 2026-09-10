#!/usr/bin/env python3
"""Bulk-archive Gmail messages, with defensive checks and reversible writes.

Two input modes:

  Mode A (preferred for non-trivial archives) — pass a decisions file that
  encodes a verdict per message:

      uv run python .agents/skills/email-digest/scripts/bulk_archive.py \\
          --decisions-from runtime/bulk_archive/ap_decisions.json \\
          --yes

  The decisions file format:

      {
          "query": "from:notifications@example.com in:inbox",
          "archive_ids": ["<id>", ...],
          "review_ids":  ["<id>", ...],
          "messages": [
              {"id": "...", "from": "...", "subject": "...", "verdict": "ARCHIVE"},
              {"id": "...", "from": "...", "subject": "...", "verdict": "REVIEW"},
              ...
          ]
      }

  This mode runs three pre-flight checks against the file itself (no Gmail
  fetches needed): (1) archive_ids ∩ review_ids must be empty; (2) every
  archive_id must have a matching messages[] record with verdict="ARCHIVE";
  (3) every archive_id's "from" field must contain the substring derived
  from the query's `from:` operator (if present).

  Mode B (quick cleanups) — pass a Gmail query plus an optional keep list:

      uv run python .agents/skills/email-digest/scripts/bulk_archive.py \\
          --query "from:notifications@example.com in:inbox" \\
          --keep-from /tmp/look_list.json \\
          --yes

  In this mode, scope is implicit in the query — we don't fetch per-message
  metadata to verify each sender, since the Gmail query already constrains
  them server-side. For extra safety, pass --expect-count to fail if the
  matched count drifts from what you expected.

Dry-run is the default. Without `--yes`, the script writes the candidate ID
list to runtime/bulk_archive/pending.json and prints a summary so you can
verify before re-running with --yes.

When --yes is set, the script writes a timestamped record file BEFORE making
any destructive call: runtime/bulk_archive/last_run_<ts>.json. Even if the
archive crashes mid-flight, the full ID list is on disk and undoable via:

      uv run python .agents/skills/email-digest/scripts/bulk_archive_undo.py last_run_<ts>.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
WORK_DIR = REPO_ROOT / "runtime/bulk_archive"


# ---------- Gmail helpers ----------

def gmail(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    args = ["latchkey", "curl", "-s", "-X", method, url]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else {}


def list_ids(query: str) -> list[str]:
    ids: list[str] = []
    page = None
    q = urllib.parse.quote(query)
    while True:
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults=500"
        if page:
            url += f"&pageToken={page}"
        res = gmail("GET", url)
        ids.extend(m["id"] for m in res.get("messages", []))
        page = res.get("nextPageToken")
        if not page:
            break
    return ids


def archive(ids: list[str]) -> None:
    """Remove INBOX from every id, batched at Gmail's 1000-per-call limit."""
    for i in range(0, len(ids), 1000):
        batch = ids[i : i + 1000]
        gmail(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            {"ids": batch, "removeLabelIds": ["INBOX"]},
        )
        print(f"  batch {i // 1000 + 1}: archived {i + len(batch)}/{len(ids)}", file=sys.stderr)


# ---------- Input parsing ----------

def load_keep_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text().strip()
    if not text:
        return set()
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        ids: set[str] = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    ids.add(item)
                elif isinstance(item, dict):
                    if "id" in item:
                        ids.add(item["id"])
                    elif "msg" in item and isinstance(item["msg"], dict) and "id" in item["msg"]:
                        ids.add(item["msg"]["id"])
        return ids
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def extract_from_substr(query: str) -> str | None:
    """Pull the sender constraint out of a Gmail query, e.g. 'from:notifications@example.com in:inbox' → 'notifications@example.com'.
    Returns None if no from: operator is present (sender-check can't run)."""
    m = re.search(r"from:(\S+)", query)
    return m.group(1).strip() if m else None


# ---------- Defensive checks ----------

class CheckFailed(Exception):
    """Raised when a pre-archive defensive check finds something wrong."""


def run_decisions_checks(
    decisions: dict[str, Any],
    expected_from_substr: str | None,
) -> tuple[list[str], list[str]]:
    """Validate the structure of a decisions file. Returns (archive_ids, review_ids).
    Raises CheckFailed if anything's wrong — abort BEFORE touching Gmail."""
    archive_ids = decisions.get("archive_ids") or []
    review_ids = decisions.get("review_ids") or []
    messages = decisions.get("messages") or []

    # Check 1: disjoint
    overlap = set(archive_ids) & set(review_ids)
    if overlap:
        raise CheckFailed(f"{len(overlap)} ids appear in BOTH archive_ids and review_ids: {list(overlap)[:3]}…")
    print("  [check 1] archive_ids ∩ review_ids = ∅  ✓", file=sys.stderr)

    # Check 2: every archive_id has a matching record with verdict=ARCHIVE
    msg_by_id = {m["id"]: m for m in messages if "id" in m}
    bad_verdict = []
    for mid in archive_ids:
        rec = msg_by_id.get(mid)
        if rec is None:
            bad_verdict.append((mid, "no record"))
        elif rec.get("verdict") != "ARCHIVE":
            bad_verdict.append((mid, f"verdict={rec.get('verdict')!r}"))
    if bad_verdict:
        raise CheckFailed(
            f"{len(bad_verdict)} archive_ids fail verdict consistency: {bad_verdict[:3]}…"
        )
    print("  [check 2] every archive_id has matching record with verdict='ARCHIVE'  ✓",
          file=sys.stderr)

    # Check 3: every archive_id's `from` contains the expected substring.
    # (When the query was `from:notifications@example.com`, every record's `from` must include `notifications@example.com`.)
    if expected_from_substr:
        bad_sender = []
        for mid in archive_ids:
            frm = msg_by_id.get(mid, {}).get("from", "")
            if expected_from_substr.lower() not in frm.lower():
                bad_sender.append((mid, frm[:50]))
        if bad_sender:
            raise CheckFailed(
                f"{len(bad_sender)} archive_ids have a `from` not containing "
                f"{expected_from_substr!r}: {bad_sender[:3]}…"
            )
        print(f"  [check 3] every archive_id's from contains {expected_from_substr!r}  ✓",
              file=sys.stderr)
    else:
        print("  [check 3] skipped — no sender substring to check against", file=sys.stderr)

    return list(archive_ids), list(review_ids)


# ---------- Run record ----------

def save_run_record(
    query: str | None,
    archived_ids: list[str],
    kept_ids: list[str] | set[str],
    note: str = "",
) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = WORK_DIR / f"last_run_{ts}.json"
    path.write_text(json.dumps({
        "query": query,
        "ran_at": ts,
        "note": note,
        "archived_count": len(archived_ids),
        "kept_count": len(kept_ids),
        "archived_ids": list(archived_ids),
        "kept_ids": sorted(kept_ids),
    }, indent=2))
    return path


# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--decisions-from", type=Path,
                     help="Path to a decisions JSON file with archive_ids, review_ids, "
                          "and messages[] records. Triggers full pre-flight checks.")
    src.add_argument("--query",
                     help="Gmail search query (e.g. 'from:notifications@example.com in:inbox'). "
                          "Use with --keep-from to subtract a keep list.")

    ap.add_argument("--keep-from", type=Path,
                    help="(query mode only) path to a keep-list file: JSON list with "
                         "{id|msg.id} entries, or plain text one ID per line.")
    ap.add_argument("--expected-from-substr",
                    help="Substring every to-archive message's `from` must contain. "
                         "Auto-derived from --query's `from:` operator if not given.")
    ap.add_argument("--expect-count", type=int,
                    help="Abort if the resolved archive count doesn't equal this. "
                         "Catches drift when re-running after the source set has changed.")
    ap.add_argument("--yes", action="store_true",
                    help="Actually archive. Without it, this is a dry run.")
    args = ap.parse_args()

    # Resolve archive_ids + kept_ids based on the input mode.
    archive_ids: list[str] = []
    kept_ids: list[str] = []
    query_for_record: str | None = None
    expected_from = args.expected_from_substr

    if args.decisions_from:
        decisions = json.loads(args.decisions_from.read_text())
        query_for_record = decisions.get("query")
        if expected_from is None and query_for_record:
            expected_from = extract_from_substr(query_for_record)
        print(f"Loaded decisions from {args.decisions_from}", file=sys.stderr)
        try:
            archive_ids, kept_ids = run_decisions_checks(decisions, expected_from)
        except CheckFailed as e:
            print(f"\nABORT: pre-flight check failed — {e}", file=sys.stderr)
            return 2
    else:
        # Query mode
        keep_ids = load_keep_ids(args.keep_from) if args.keep_from else set()
        if args.keep_from:
            print(f"Loaded {len(keep_ids)} keep-ids from {args.keep_from}", file=sys.stderr)
        all_ids = list_ids(args.query)
        print(f"Gmail query matched {len(all_ids)} messages", file=sys.stderr)
        archive_ids = [i for i in all_ids if i not in keep_ids]
        kept_ids = sorted(keep_ids)
        query_for_record = args.query
        if expected_from is None:
            expected_from = extract_from_substr(args.query)
        # In query mode, sender check requires metadata fetch per id. Skip unless
        # caller wants it — but warn if --expected-from-substr was set.
        if expected_from:
            print(f"  (sender check skipped in --query mode — query is the constraint, "
                  f"expected substring would be {expected_from!r})",
                  file=sys.stderr)

    print(f"  → would archive: {len(archive_ids)}", file=sys.stderr)
    print(f"  → would keep:    {len(kept_ids)}", file=sys.stderr)

    # Count drift check
    if args.expect_count is not None and len(archive_ids) != args.expect_count:
        print(f"\nABORT: expected {args.expect_count} archive ids, got {len(archive_ids)}",
              file=sys.stderr)
        return 2

    # Always write pending.json (overwritten each run) so the caller can review.
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    pending = WORK_DIR / "pending.json"
    pending.write_text(json.dumps({
        "query": query_for_record,
        "would_archive_count": len(archive_ids),
        "would_archive_ids": archive_ids,
        "kept_count": len(kept_ids),
        "kept_ids": kept_ids,
    }, indent=2))
    print(f"  → pending list written to {pending}", file=sys.stderr)

    if not args.yes:
        print(file=sys.stderr)
        print("Dry run only. Re-run with --yes to actually archive.", file=sys.stderr)
        return 0

    if not archive_ids:
        print("Nothing to archive.", file=sys.stderr)
        return 0

    # CRITICAL: write the run record BEFORE the first destructive call.
    # This guarantees that even if archive crashes mid-flight, the full ID
    # list is on disk and undoable.
    record_path = save_run_record(
        query_for_record, archive_ids, kept_ids,
        note=("from decisions file" if args.decisions_from else "from --query"),
    )
    print(f"  → undo record saved to {record_path} (BEFORE archive)", file=sys.stderr)

    print(f"\nArchiving {len(archive_ids)} messages…", file=sys.stderr)
    archive(archive_ids)
    print(f"\nDone. Undo: bulk_archive_undo.py {record_path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
