#!/usr/bin/env python3
"""Contact audit — find people the user has corresponded with that aren't in
contacts.txt yet, so future cold-detection doesn't wrongly route them to
bucket 6.

Pipeline:

  1. Pull every message the user has SENT in the last N days (default 180)
     via Gmail API.
  2. Extract unique recipient addresses (To + Cc), filtering out:
       - The user's own addresses (account.ACCOUNT_ADDRS)
       - Already-known contacts (contacts.txt)
       - Org staff (account.ORG_DOMAINS — routed automatically)
       - Automated senders / unsubscribe handlers (regex)
  3. Tier candidates by how many times the user emailed each in the window:
       - 3+ emails: auto-include as genuine.
       - 1-2 emails: send to LLM judge in batches.
  4. LLM judge marks each 1-2-email candidate GENUINE or NOT_GENUINE.
     NOT_GENUINE with a mass-invite subject is added back — the user likely
     knows these people; lack of reply on the bulk invite doesn't mean cold.
  5. Output:
       - `--dry-run` (default): write proposed_contacts.md for review.
       - `--write`: append entries to contacts.txt under a dated section.

Usage:
    uv run python .agents/skills/email-digest/scripts/contact_audit.py
    uv run python .agents/skills/email-digest/scripts/contact_audit.py --write
    uv run python .agents/skills/email-digest/scripts/contact_audit.py --days 365
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from email_review.account import ACCOUNT_ADDRS, ORG_DOMAINS

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTACTS_PATH = REPO_ROOT / ".agents/skills/email-digest/contacts.txt"
WORK_DIR = REPO_ROOT / "runtime/contact_audit"

AUTO_PATTERNS = re.compile(
    r"(noreply|no-reply|notifications?@|donotreply|mailer-daemon|"
    r"^receipts@|^billing@|^invoice|@docusign|invoice\+|"
    r"^lp-relations@|^investorservices@|^accounts?@|^support@|"
    r"@calendar-server|@mail\.google\.com|^announce@|^team@)",
    re.I,
)
UNSUB_HANDLER = re.compile(
    r"unsubscribe|unsub-|mktomail|hubspotemail|loops\.so|customer\.io|"
    r"qemailserver",
    re.I,
)
MASS_INVITE_SUBJECTS = re.compile(
    r"^(re:\s*)?(controlling x|event on thursday|event tomorrow|"
    r"new tool to filter x|tool for filtering x)\b",
    re.I,
)


def gmail(url: str) -> dict[str, Any]:
    out = subprocess.run(
        ["latchkey", "curl", "-s", url],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out) if out.strip() else {}


def read_own_addrs() -> set[str]:
    """Your own addresses (from account.py) so the audit skips them."""
    return {a.lower() for a in ACCOUNT_ADDRS}


def read_existing_contacts() -> set[str]:
    """Return the set of email addresses (lowercased) and bare domains already
    listed in contacts.txt."""
    out: set[str] = set()
    if not CONTACTS_PATH.exists():
        return out
    for line in CONTACTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if not parts:
            continue
        # A row may list several comma-separated addresses for one person.
        for addr in parts[0].strip().lower().split(","):
            addr = addr.strip()
            if addr:
                out.add(addr)
    return out


def pull_sent_mail(days: int) -> list[dict[str, Any]]:
    ids: list[str] = []
    page = None
    while True:
        url = (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            f"?q=in%3Asent+newer_than%3A{days}d&maxResults=500"
        )
        if page:
            url += f"&pageToken={page}"
        res = gmail(url)
        ids.extend(m["id"] for m in res.get("messages", []))
        page = res.get("nextPageToken")
        if not page:
            break
    print(f"Pulled {len(ids)} sent message IDs", file=sys.stderr)

    def fetch(mid: str) -> dict[str, Any]:
        url = (
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
            f"?format=metadata&metadataHeaders=To&metadataHeaders=Cc"
            f"&metadataHeaders=Subject&metadataHeaders=Date"
        )
        d = gmail(url)
        h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
        return {
            "id": mid,
            "threadId": d.get("threadId"),
            "to": h.get("to", ""),
            "cc": h.get("cc", ""),
            "subject": h.get("subject", ""),
            "date": h.get("date", ""),
        }

    t0 = time.time()
    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(fetch, ids):
            records.append(r)
    print(f"Metadata fetched in {time.time()-t0:.1f}s", file=sys.stderr)
    return records


def extract_candidates(
    records: list[dict[str, Any]],
    skip_addrs: set[str],
    skip_domains: set[str],
) -> tuple[dict[str, int], dict[str, list[dict[str, str]]]]:
    freq: dict[str, int] = {}
    samples: dict[str, list[dict[str, str]]] = {}
    for r in records:
        to_cc = (r.get("to", "") + ", " + r.get("cc", "")).lower()
        addrs = re.findall(r"[\w\.\-\+]+@[\w\.\-]+", to_cc)
        for a in addrs:
            if a in skip_addrs:
                continue
            if AUTO_PATTERNS.search(a) or UNSUB_HANDLER.search(a):
                continue
            domain = a.split("@", 1)[1] if "@" in a else ""
            if domain in skip_domains or domain in ORG_DOMAINS:
                continue
            freq[a] = freq.get(a, 0) + 1
            samples.setdefault(a, []).append({
                "subject": r.get("subject", ""),
                "date": r.get("date", "")[:25],
                "threadId": r["threadId"],
            })
    return freq, samples


def llm_judge_batch(
    targets: list[tuple[str, int]],
    samples: dict[str, list[dict[str, str]]],
    batch_size: int = 110,
) -> dict[str, tuple[str, str]]:
    """Return {addr: (verdict, reason)} where verdict is GENUINE or NOT_GENUINE."""
    header = (
        "You are reviewing email addresses the user has emailed in the last "
        "180 days. For each, output exactly one line:\n"
        "  [N] GENUINE — <reason>\n"
        "or:\n"
        "  [N] NOT_GENUINE — <reason>\n\n"
        "Mark NOT_GENUINE if the address is automated / unsubscribe-handler "
        "(no person), or the subject suggests a cold pitch / spam / mass-invite "
        "the recipient didn't engage with. Mark GENUINE for real conversations, "
        "intros that turned into engagement, social/work threads. Be conservative "
        "— when unsure, GENUINE.\n\n"
        "=== Candidates ===\n"
    )
    verdicts: dict[str, tuple[str, str]] = {}
    batches = [targets[i:i+batch_size] for i in range(0, len(targets), batch_size)]
    for b_idx, batch in enumerate(batches):
        print(f"  batch {b_idx+1}/{len(batches)} ({len(batch)} candidates)…",
              file=sys.stderr)
        lines = []
        for local_idx, (addr, n) in enumerate(batch):
            subs = [s["subject"][:70] for s in samples.get(addr, [])][:2]
            lines.append(
                f"[{local_idx}] {addr} (emailed {n}x) — subjects: " +
                " | ".join(f'"{s}"' for s in subs)
            )
        prompt = header + "\n".join(lines)
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=240, check=False,
        )
        if result.returncode != 0:
            print(f"    judge call failed: {result.stderr[-200:]}", file=sys.stderr)
            continue
        for line in result.stdout.splitlines():
            m = re.match(
                r"^\s*\[(\d+)\]\s*(GENUINE|NOT_GENUINE)\s*[—–-]?\s*(.*)$",
                line,
            )
            if not m:
                continue
            local_idx = int(m.group(1))
            if local_idx >= len(batch):
                continue
            addr, _ = batch[local_idx]
            verdicts[addr] = (m.group(2), m.group(3).strip())
    return verdicts


def name_from(addr: str) -> str:
    local = addr.split("@", 1)[0]
    local = re.sub(r"\d+$", "", local)
    parts = re.split(r"[._\-]+", local)
    return " ".join(p.capitalize() for p in parts if p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180,
                    help="how many days of sent mail to scan (default 180)")
    ap.add_argument("--write", action="store_true",
                    help="append entries to contacts.txt (default: dry-run, just print)")
    ap.add_argument("--skip-llm", action="store_true",
                    help="skip the LLM judge pass (only return the 3+ frequency tier)")
    args = ap.parse_args()

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    skip_addrs = read_own_addrs() | read_existing_contacts()
    skip_domains = {d for d in skip_addrs if "@" not in d}

    records = pull_sent_mail(args.days)
    (WORK_DIR / "sent_records.json").write_text(json.dumps(records))

    freq, samples = extract_candidates(records, skip_addrs, skip_domains)
    print(f"Unique candidate addresses (after filtering): {len(freq)}",
          file=sys.stderr)

    top_tier = [(a, n) for a, n in freq.items() if n >= 3]
    long_tail = sorted(
        [(a, n) for a, n in freq.items() if n <= 2],
        key=lambda kv: (-kv[1], kv[0]),
    )
    print(f"  3+ tier (auto-include): {len(top_tier)}", file=sys.stderr)
    print(f"  1-2 tier (LLM judged): {len(long_tail)}", file=sys.stderr)

    verdicts: dict[str, tuple[str, str]] = {}
    if long_tail and not args.skip_llm:
        verdicts = llm_judge_batch(long_tail, samples)

    # Build final accept set
    accepted: dict[str, dict[str, Any]] = {}
    for addr, n in top_tier:
        accepted[addr] = {"n": n, "note": f"emailed {n}x in {args.days}d"}
    for addr, n in long_tail:
        v = verdicts.get(addr)
        if v is None:
            continue  # judge omitted — skip
        verdict, reason = v
        if verdict == "GENUINE":
            accepted[addr] = {"n": n, "note": f"emailed {n}x, judge: {reason[:80]}"}
        elif verdict == "NOT_GENUINE":
            # Mass-invite carve-out
            subjects = [s["subject"] for s in samples.get(addr, [])]
            if any(MASS_INVITE_SUBJECTS.match(s or "") for s in subjects):
                accepted[addr] = {
                    "n": n,
                    "note": "recipient of your mass invite (bulk event/announcement)",
                }

    print(f"Accepted: {len(accepted)} new contacts", file=sys.stderr)

    # Build review/output
    today = date.today().isoformat()
    review_path = WORK_DIR / f"proposed_contacts_{today}.md"
    lines = [
        f"# Proposed Contacts (audit {today})",
        "",
        f"Total: {len(accepted)} new candidates (scanned {args.days} days of sent mail).",
        "All proposed as **trusted-warm**.",
        "",
    ]
    for addr in sorted(accepted, key=lambda a: (-accepted[a]["n"], a)):
        e = accepted[addr]
        lines.append(f"- `{addr}` — {e['note']}")
    review_path.write_text("\n".join(lines))
    print(f"Wrote review to {review_path}", file=sys.stderr)

    if args.write:
        section = (
            f"\n# --- Contact audit ({today}) ---\n"
            "# Auto-imported by contact_audit.py from your outbound mail.\n"
        )
        with CONTACTS_PATH.open("a") as f:
            f.write(section)
            for addr in sorted(accepted):
                e = accepted[addr]
                f.write(
                    f"{addr}\t{name_from(addr)}\ttrusted-warm\t"
                    f"From contact audit ({today}) — {e['note']}\n"
                )
        print(f"Appended {len(accepted)} entries to {CONTACTS_PATH}",
              file=sys.stderr)
    else:
        print("(dry run — pass --write to append to contacts.txt)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
