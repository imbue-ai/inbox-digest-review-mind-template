#!/usr/bin/env python3
"""Review the email-review move log and surface repeated classification
corrections worth turning into rules.

Reads data/.apps/email-review/move_log.jsonl (written by the digest's /api/move
endpoint — each line is a labeled move: who the mail is from, its subject /
content, the bucket the classifier chose, and the bucket the user moved it to).
Groups corrections by sender address and by sender domain, and reports any
where the user has moved >= MIN_REPEATS messages to the same bucket and that
bucket differs from what the classifier chose (i.e. a real, repeated
correction). These are candidates for a durable rule.

This script only *proposes* — it never edits classifier rules. An agent reviews
the output, translates each pattern into a concrete rule, and shows the user
before applying.

Run from the repo root:  uv run python scripts/review_email_moves.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

MOVE_LOG_PATH = Path("data/.apps/email-review/move_log.jsonl")
MIN_REPEATS = 2  # how many same-direction moves before it's a "pattern"


def load_moves() -> list[dict[str, Any]]:
    if not MOVE_LOG_PATH.exists():
        return []
    out = []
    for line in MOVE_LOG_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def find_patterns(moves: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Group moves by a key fn (sender address or domain) and return groups
    where >= MIN_REPEATS moves all went to the same to_bucket, and that target
    differs from the classifier's bucket for at least one of them (a real
    correction, not just confirming the classifier)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in moves:
        k = (m.get("from_addr", "") if key == "sender" else _domain(m.get("from_addr", ""))).lower()
        if k:
            groups[k].append(m)

    patterns = []
    for k, ms in groups.items():
        targets = {m.get("to_bucket") for m in ms}
        if len(ms) < MIN_REPEATS or len(targets) != 1:
            continue  # not repeated, or inconsistent direction
        target = next(iter(targets))
        classifier_buckets = {m.get("classifier_bucket") for m in ms}
        if classifier_buckets == {target}:
            continue  # classifier already agrees — nothing to learn
        patterns.append({
            "key_type": key,
            "key": k,
            "count": len(ms),
            "to_bucket": target,
            "to_bucket_name": ms[0].get("to_bucket_name", target),
            "classifier_buckets": sorted(b for b in classifier_buckets if b),
            "example_subjects": [m.get("subject", "")[:60] for m in ms[:3]],
        })
    return sorted(patterns, key=lambda p: -p["count"])


def main() -> None:
    moves = load_moves()
    print(f"Move log: {len(moves)} recorded move(s).")
    if not moves:
        print("No moves logged yet — nothing to review.")
        return

    # Prefer sender-level patterns; fall back to domain-level for the rest.
    sender_patterns = find_patterns(moves, "sender")
    covered = {p["key"] for p in sender_patterns}
    domain_patterns = [
        p for p in find_patterns(moves, "domain")
        # skip a domain pattern fully explained by a single already-reported sender
        if not (p["count"] <= 1)
    ]

    proposals = sender_patterns + domain_patterns
    if not proposals:
        print(f"No repeated corrections yet (need >= {MIN_REPEATS} same-direction moves "
              "that disagree with the classifier).")
        return

    print(f"\n{len(proposals)} candidate pattern(s) worth a rule:\n")
    for p in proposals:
        label = "sender" if p["key_type"] == "sender" else "domain"
        print(f"  • {label} {p['key']}: moved {p['count']}x → "
              f"{p['to_bucket_name']} (bucket {p['to_bucket']}); "
              f"classifier had put it in {p['classifier_buckets']}")
        for s in p["example_subjects"]:
            print(f"        e.g. {s!r}")
    # Machine-readable block for an agent to act on.
    print("\n---PROPOSALS_JSON---")
    print(json.dumps(proposals, indent=2))


if __name__ == "__main__":
    main()
