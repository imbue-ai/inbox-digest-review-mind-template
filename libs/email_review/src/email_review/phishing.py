"""Shared phishing-detection rules.

Both `classify.py` (which assigns a bucket) and `gmail_actions.py` (which
decides whether smart-action should mark spam vs unsubscribe) need the same
signal: is this message likely phishing? Keeping the rules in one place
prevents drift — fixing one detection gap auto-fixes the other.

Public surface:
    OWN_ADDRS                 — set of your verified own addresses
    PHISHING_SUBJ_PAT         — compiled subject regex
    PHISHING_BODY_PAT         — compiled body/snippet regex
    has_phishing_tells(...)   — top-level check used by both callers
"""

from __future__ import annotations

import re

from email_review.account import ACCOUNT_ADDRS, ACCOUNT_NAME

# Your own addresses — mail from these is never treated as impersonation.
OWN_ADDRS: set[str] = set(ACCOUNT_ADDRS)

# The account owner's name, as an alternation that matches either the full name
# or just the first name, so "asked by Alex" and "asked by Alex Doe" both hit.
_NAME_PARTS = ACCOUNT_NAME.split()
_NAME_ALT = re.escape(ACCOUNT_NAME)
if len(_NAME_PARTS) > 1:
    _NAME_ALT = rf"{re.escape(ACCOUNT_NAME)}|{re.escape(_NAME_PARTS[0])}"

# Suspicious SUBJECT patterns. Anchored loosely (no `^`) so an invoice token
# embedded later in a long subject still matches.
PHISHING_SUBJ_PAT = re.compile(
    # Signature-phishing classics with hex-hash suffix (the original incident
    # that motivated the detector — "Re: Shares Allocation<8-hex>" etc.).
    r"\bRe:\s*(?:Shares\s+Allocation|Shareholders?\s+Consent|Invoice\s+\d{4,})[a-f0-9]{8,}|"
    # Invoice/PO tokens with numbers + various separators anywhere in subject.
    # Catches "#INV-2026-051", "INV#1100000888889q5l1", "PO-12345-ABC", etc.
    r"#?(?:INV|PO|INVOICE|PURCHASE\s*ORDER)[-_\s]?#?\s*\d{3,}[\w\-]*|"
    # Gift card scams.
    r"\b(?:google play|amazon|apple|itunes)\s+gift\s*card\b|"
    # Signature-request spoofing.
    r"signature\s+(?:request(?:ed|s)?|required)|"
    # Wire-transfer urgency.
    r"\burgent\s+(?:wire|payment|transfer)\b",
    re.I,
)

# Body/snippet phishing tells. The strongest signal in invoice-fraud emails is
# name-dropping the account owner in the body to claim authority while the
# sender isn't them.
PHISHING_BODY_PAT = re.compile(
    # "I have been asked by <you> to forward..." / "<you> asked me to send..." —
    # classic AP-fraud social engineering.
    rf"(asked|requested|instructed)\s+by\s+(?:{_NAME_ALT})\b|"
    rf"(?:{_NAME_ALT})\s+(asked|requested|instructed)\s+(me|us)\s+to\b|"
    rf"on\s+behalf\s+of\s+(?:{_NAME_ALT})\b|"
    # Other classic wire-fraud phrases.
    r"please\s+process\s+the\s+attached\s+invoice|"
    r"\bwire\s+transfer\b|"
    r"remittance\s+(advice|details)\s+attached|"
    r"forward\s+the\s+attached\s+invoice",
    re.I,
)

# Lowercased forms used for the substring / display-name checks below.
_NAME_LOWER = ACCOUNT_NAME.lower()
_FIRST_LOWER = _NAME_PARTS[0].lower()


def _parse_display(frm: str) -> str:
    """Extract the display name from a `From` header. Returns empty string if
    the header is just a bare address."""
    if not frm:
        return ""
    # "Display Name <addr@host>"  →  "Display Name"
    if "<" in frm:
        return frm.split("<", 1)[0].strip().strip('"')
    return ""


def _parse_addr(frm: str) -> str:
    if not frm:
        return ""
    m = re.search(r"<([^>]+)>", frm)
    return (m.group(1) if m else frm).strip().lower()


def has_phishing_tells(subject: str, frm: str, snippet: str, addr: str | None = None) -> bool:
    """Return True if the message has obvious phishing signals. Conservative:
    each signal alone is enough to mark a message phishing; the union is
    designed to be high-precision (rare false positives).

    Signals:
      1. Display-name says your name but the From address isn't yours.
      2. Subject matches PHISHING_SUBJ_PAT (invoice tokens, signature
         requests, gift-card scams, urgent-wire phrasing).
      3. Body/snippet name-drops you while the sender isn't you.
    """
    addr = (addr or _parse_addr(frm)).lower()
    display = _parse_display(frm).lower()

    # 1. Display-name impersonation
    if _NAME_LOWER in display and addr not in OWN_ADDRS:
        return True

    # 2. Subject patterns
    if PHISHING_SUBJ_PAT.search(subject or ""):
        return True

    # 3. Body name-drop or wire-fraud language. Only flag if the sender is NOT
    # you (you might say your own name in a forwarded thread).
    if snippet and addr not in OWN_ADDRS and PHISHING_BODY_PAT.search(snippet):
        return True

    return False
