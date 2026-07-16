#!/usr/bin/env python3
"""LLM-judge pass over the classifier output.

After classify.py produces runtime/email_review/data.json, this script
re-evaluates every bucket-1 (Reply needed) and bucket-3 (FYI) thread using
Claude as a judge against the rules in
runtime/memory/feedback_self_review_classifications.md. It applies the
judge's verdict by updating final_bucket and final_why on every message in
the thread.

Why: deterministic regex rules in classify.py catch ~90% of the obvious
patterns, but judgment calls (forwarded info with no specific ask,
sender taking action themselves, brokered scheduling, etc.) need an LLM.

Performance: all threads go to Claude in one batched call so the latency
is one model round-trip (~30s) rather than per-thread Claude Code starts.

Usage:
    uv run python .agents/skills/email-digest/scripts/llm_judge.py

Reads/writes runtime/email_review/data.json in place. Exits non-zero only
on hard failure (claude CLI missing, malformed JSON). Per-thread parse
errors are logged to stderr and the original bucket is kept.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_PATH = REPO_ROOT / "runtime/email_review/data.json"
FEEDBACK_PATH = REPO_ROOT / "runtime/memory/feedback_self_review_classifications.md"

ALLOWED_BUCKETS = {"1", "3", "5", "8", "10"}

BATCH_PROMPT_HEADER = """\
You are reviewing email bucket assignments. Buckets:
  1 = Reply needed (the user has a specific ask to answer)
  3 = FYI / read (no action — ack, info-only, brokered, resolved)
  5 = Sent / awaiting reply (the user sent, waiting on response)
  8 = In-product notifications (calendar invites/updates, SaaS notifications,
       meeting recap bots, automated tool emails)
  10 = Work FYI (automated AP / vendor / process mail — bill-pay, invoices,
       reimbursements addressed to your AP forwarder)

=== Self-review rules (canonical) ===
{rules}

=== Threads to review ===
{threads}

=== Output format ===
Respond with EXACTLY one line per thread, in the order given, no preamble
or trailing commentary. Each line is one of:

  [<thread_index>] KEEP — <short reason>
  [<thread_index>] MOVE_TO_<n> — <short reason>

where <n> is one of 1, 3, 5, 10 and <thread_index> matches the bracketed
number in the input. KEEP when the current bucket is right. MOVE_TO only
when the rules clearly indicate a different bucket. Conservative default:
KEEP if unsure (false retentions are cheap; false demotions hide things
the user wanted to see).
"""


def load_data() -> dict[str, Any]:
    return json.loads(DATA_PATH.read_text())


def load_rules() -> str:
    if not FEEDBACK_PATH.exists():
        return "(no feedback memory found — fall back to default judgment)"
    text = FEEDBACK_PATH.read_text()
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)


def format_message(m: dict[str, Any]) -> str:
    snip = (m.get("snippet") or "").strip()
    snip = re.split(r"on .+ wrote:|On .+ wrote:", snip, maxsplit=1)[0].strip()
    if len(snip) > 250:
        snip = snip[:250] + "…"
    return (
        f"    From: {m.get('from', '')[:60]}\n"
        f"    To:   {m.get('to', '')[:80]}\n"
        f"    Cc:   {m.get('cc', '')[:80]}\n"
        f"    Body: {snip}"
    )


def format_thread(idx: int, thread_msgs: list[dict[str, Any]]) -> str:
    thread_msgs = sorted(thread_msgs, key=lambda r: r.get("date", ""))
    current = thread_msgs[0]["final_bucket"]
    subject = thread_msgs[0].get("subject", "")[:120]
    body = "\n    ---\n".join(format_message(m) for m in thread_msgs)
    return (
        f"[{idx}] current_bucket={current} ({len(thread_msgs)} msg)\n"
        f"  Subject: {subject}\n"
        f"{body}"
    )


_VERDICT_RE = re.compile(
    r"^\s*\[(?P<idx>\d+)\]\s*(KEEP|MOVE_TO_(?P<bucket>\d+))\s*[—–-]?\s*(?P<reason>.*?)\s*$",
)


def parse_batch_verdicts(out: str, expected_idxs: list[int]) -> dict[int, tuple[str | None, str]]:
    """Return {idx: (target_bucket or None, reason)} for each parsed verdict.
    Missing or unparseable idxs are omitted — caller treats them as KEEP."""
    verdicts: dict[int, tuple[str | None, str]] = {}
    for line in out.splitlines():
        m = _VERDICT_RE.match(line)
        if not m:
            continue
        idx = int(m.group("idx"))
        if idx not in expected_idxs:
            continue
        if "KEEP" in line.split("—", 1)[0]:
            verdicts[idx] = (None, m.group("reason") or "judge kept current bucket")
        else:
            bucket = m.group("bucket")
            if bucket not in ALLOWED_BUCKETS:
                print(
                    f"  judge proposed unallowed bucket {bucket!r} for thread [{idx}] — keeping",
                    file=sys.stderr,
                )
                verdicts[idx] = (None, "judge proposed unallowed bucket")
            else:
                verdicts[idx] = (bucket, m.group("reason") or f"moved to bucket {bucket}")
    return verdicts


def call_claude(prompt: str, timeout_sec: int = 180) -> str:
    """Single call to claude -p with the full batch prompt."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude returned {result.returncode}: {result.stderr[-300:]}"
        )
    return result.stdout


def main() -> int:
    data = load_data()
    rules = load_rules()

    by_thread: dict[str, list[dict[str, Any]]] = {}
    for m in data.get("messages", []):
        by_thread.setdefault(m["threadId"], []).append(m)

    # Stable order so the indices line up.
    candidates = sorted(
        (msgs for msgs in by_thread.values()
         if msgs and msgs[0].get("final_bucket") in {"1", "3"}),
        key=lambda msgs: msgs[0]["threadId"],
    )

    if not candidates:
        print("No bucket-1 or bucket-3 threads to judge.", file=sys.stderr)
        data.setdefault("stats", {})["llm_judge_moves"] = 0
        data["stats"]["llm_judge_log"] = []
        DATA_PATH.write_text(json.dumps(data, indent=2))
        return 0

    # Judge in parallel chunks. Each thread's verdict is independent (every
    # output line is judged against the same canonical rules using only that
    # thread's own content), so splitting the batch into concurrent chunks is
    # quality-neutral — identical model (Opus), identical rules, identical
    # per-thread decision — while cutting wall-clock from one long sequential
    # Opus pass to roughly the slowest single chunk. Global thread indices are
    # preserved across chunks so verdicts merge unambiguously.
    n = len(candidates)
    MAX_PARALLEL = 6
    # Aim to spread across up to MAX_PARALLEL concurrent chunks. A small floor
    # keeps parallelism kicking in for modest counts (each claude -p call has
    # high fixed latency, so even a chunk of ~6 threads is worth splitting).
    chunk_size = max(6, -(-n // MAX_PARALLEL))  # ceil(n / MAX_PARALLEL), min 6
    idx_chunks = [
        list(range(i, min(i + chunk_size, n))) for i in range(0, n, chunk_size)
    ]
    max_workers = min(MAX_PARALLEL, len(idx_chunks))
    print(
        f"Judging {n} threads across {len(idx_chunks)} chunk(s), "
        f"{max_workers} concurrent…",
        file=sys.stderr,
    )

    def run_chunk(idxs: list[int]) -> dict[int, tuple[str | None, str]]:
        block = "\n\n".join(format_thread(i, candidates[i]) for i in idxs)
        prompt = BATCH_PROMPT_HEADER.format(rules=rules, threads=block)
        try:
            return parse_batch_verdicts(call_claude(prompt), idxs)
        except (RuntimeError, subprocess.SubprocessError) as e:
            print(
                f"  judge chunk [{idxs[0]}..{idxs[-1]}] failed ({e}); "
                f"keeping those threads' buckets",
                file=sys.stderr,
            )
            return {}

    verdicts: dict[int, tuple[str | None, str]] = {}
    if max_workers <= 1:
        for idxs in idx_chunks:
            verdicts.update(run_chunk(idxs))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for part in ex.map(run_chunk, idx_chunks):
                verdicts.update(part)

    if not verdicts:
        # Every chunk failed (e.g. claude CLI missing) — surface a hard error
        # rather than silently reporting "0 moves".
        print("  judge produced no verdicts (all chunks failed)", file=sys.stderr)
        return 1

    moves = 0
    judge_log: list[dict[str, Any]] = []
    for idx, thread_msgs in enumerate(candidates):
        target, reason = verdicts.get(idx, (None, "judge omitted — keeping"))
        was_bucket = thread_msgs[0]["final_bucket"]
        if target is None or target == was_bucket:
            judge_log.append(
                {
                    "threadId": thread_msgs[0]["threadId"],
                    "subject": thread_msgs[0].get("subject", "")[:80],
                    "verdict": "KEEP",
                    "from_bucket": was_bucket,
                    "reason": reason,
                }
            )
            continue
        for r in thread_msgs:
            r["final_bucket"] = target
            r["final_why"] = f"LLM judge: {reason} (was bucket {was_bucket})"
        moves += 1
        judge_log.append(
            {
                "threadId": thread_msgs[0]["threadId"],
                "subject": thread_msgs[0].get("subject", "")[:80],
                "verdict": f"MOVE_TO_{target}",
                "from_bucket": was_bucket,
                "reason": reason,
            }
        )

    data.setdefault("stats", {})["llm_judge_moves"] = moves
    data["stats"]["llm_judge_log"] = judge_log
    DATA_PATH.write_text(json.dumps(data, indent=2))

    print(
        f"LLM judge applied {moves} move(s); {len(judge_log) - moves} kept.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
