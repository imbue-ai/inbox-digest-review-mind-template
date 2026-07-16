#!/usr/bin/env python3
"""Undo a previous bulk_archive run by re-adding INBOX to every archived id.

Usage:
    uv run python .agents/skills/email-digest/scripts/bulk_archive_undo.py last_run_20260517-203000.json

Pass the filename of a record from runtime/bulk_archive/. The script re-adds
the INBOX label to every id listed in `archived_ids`. Safe to re-run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
WORK_DIR = REPO_ROOT / "runtime/bulk_archive"


def gmail(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    args = ["latchkey", "curl", "-s", "-X", method, url]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else {}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    name = sys.argv[1]
    path = Path(name) if Path(name).is_absolute() else WORK_DIR / name
    if not path.exists():
        print(f"Run record not found: {path}", file=sys.stderr)
        return 1
    record = json.loads(path.read_text())
    ids = record.get("archived_ids", [])
    print(f"Restoring {len(ids)} messages to INBOX (from query: {record.get('query')!r})…",
          file=sys.stderr)
    for i in range(0, len(ids), 1000):
        batch = ids[i : i + 1000]
        gmail(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            {"ids": batch, "addLabelIds": ["INBOX"]},
        )
        print(f"  batch {i // 1000 + 1}: restored {i + len(batch)}/{len(ids)}",
              file=sys.stderr)
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
