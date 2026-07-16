#!/usr/bin/env python3
"""Write a one-line synthesis for each message in runtime/email_review/data.json.

Synthesis format: "{Sender (optional org)} — {topic} {optional context}"
Hand-written for human items; templated for AP-forwarded automation.
"""

import json
import re
from pathlib import Path

DATA = Path("runtime/email_review/data.json")

# Optional hand-written synthesis per Gmail message ID. Fill this in when the
# auto-generated "{Sender} — {subject}" line for a specific message isn't good
# enough; the key is the message's Gmail id and the value is the one-liner to
# show instead. Ships empty — every message falls back to sender + subject
# until you add an override.
#
# Example:
#   SYN = {"18f0a1b2c3d4e5f6": "Acme Corp — Q3 invoice, needs your approval"}
SYN: dict[str, str] = {}


def main() -> None:
    data = json.loads(DATA.read_text())
    missing = []
    for m in data["messages"]:
        mid = m["id"]
        if mid in SYN:
            m["synthesis"] = SYN[mid]
        else:
            # Fallback: sender + subject
            from_addr = m.get("from_addr", "")
            sender = re.sub(r"\s*<.*", "", m.get("from", "")).strip().strip('"') or from_addr
            subj = re.sub(r"^(Re:\s*|Fwd:\s*)+", "", m.get("subject", ""), flags=re.I)
            m["synthesis"] = f"{sender} — {subj}"
            missing.append((mid, m["final_bucket"], m["subject"][:60]))
    DATA.write_text(json.dumps(data, indent=2))
    if missing:
        print(f"{len(missing)} fallback synth (no hand-written line):")
        for mid, b, s in missing:
            print(f"  bucket {b} | {mid} | {s}")
    print(f"Wrote {len(data['messages'])} synthesis lines.")


if __name__ == "__main__":
    main()
