#!/usr/bin/env python3
"""Classify the N most recent unarchived Gmail messages per the email-digest skill rules.

Usage:
    uv run .agents/skills/email-digest/scripts/classify.py [N] [--offset OFFSET]

Defaults to N=100. Writes JSON to data/.apps/email-review/data.json so the
email-review web service picks it up. The rules implemented here mirror
.agents/skills/email-digest/RULES.md — when the rules change there, this
script must change too. Includes the mandatory self-review pass per
feedback_self_review_classifications.md.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from email_review.account import (
    ACCOUNT_ADDRS,
    ACCOUNT_FIRST_NAME,
    AP_FORWARDER_ADDRS,
    ORG_DOMAINS,
)

# ---- Gmail / latchkey helpers ----


class GatewayUnreachable(SystemExit):
    """Raised (as a clean exit) when the latchkey gateway can't be reached, so
    the refresh reports a human message instead of a raw subprocess traceback."""


def fetch(url: str, retries: int = 3) -> dict[str, Any]:
    """Fetch a Gmail API URL through latchkey. Retries transient failures (the
    host-side latchkey gateway occasionally blips), and on a persistent failure
    raises GatewayUnreachable with a clear message rather than crashing with a
    CalledProcessError traceback."""
    last = None
    for attempt in range(retries):
        last = subprocess.run(["latchkey", "curl", "-s", url], capture_output=True, text=True, check=False)
        if last.returncode == 0:
            try:
                return json.loads(last.stdout)
            except json.JSONDecodeError:
                return {}
        time.sleep(1.5 * (attempt + 1))
    raise GatewayUnreachable(
        f"Gmail is unreachable via latchkey (curl exit {last.returncode}). The latchkey "
        "gateway (the Gmail credential proxy) appears to be down — this is a workspace/host "
        "connection issue, not the digest itself. Retry once the workspace connection is restored."
    )

# ---- Gmail native category labels ----
#
# Gmail auto-applies these system labels; the pre-filter uses them as starting
# hints (the content pass can override). No external mail client required.
CATEGORY_PROMOTIONS = "CATEGORY_PROMOTIONS"   # marketing / promotional → bucket 7
CATEGORY_UPDATES = "CATEGORY_UPDATES"         # confirmations, receipts, notifications → bucket 8
CATEGORY_FORUMS = "CATEGORY_FORUMS"           # mailing lists / newsletters → bucket 9
CATEGORY_SOCIAL = "CATEGORY_SOCIAL"           # social-network notifications → bucket 8

# ---- Maintained lists (mirror RULES.md) ----

# Patterns that identify a sender as automated. Detection principle:
#
#   PREFER LOCAL-PART PATTERNS over domains.  Mailboxes like
#   "noreply@", "notifications@", "team@", "news@" are reliably automation
#   regardless of which domain they live on. They catch the SaaS
#   notification mailbox at any company while leaving humans at that
#   company alone.
#
#   AVOID WHOLE-DOMAIN MATCHES.  Even "obviously SaaS" companies (Stripe,
#   Linear, Sanity, Fireflies, Quickbooks, Docusign, Intercom...) employ
#   real humans you might email with. The general rule of thumb is:
#   ANY company with employees you could conceivably meet at a conference
#   or get an intro to has humans on its main domain. Domain-level
#   whitelists for those companies will silently route real people into
#   bucket 8.
#
#   The ONLY domains it is safe to whitelist whole are:
#     1. Dedicated transactional / notification subdomains that no human
#        ever sends from (e.g. `email.<company>.com`, `notify.<x>.com`).
#        These are typically used solely by the company's email service
#        provider — never by individual employees.
#     2. System-bot domains where the entire domain is one automated
#        sender (e.g. `calendar.google.com` — only the Google Calendar
#        invitation bot uses this).
#
#   For everything else, list the specific noreply mailbox (e.g.
#   `noreply@linear.app`) so the whitelist is targeted.
#
# The example entries below are common SaaS automation mailboxes — treat them
# as a starting point and extend for your own inbox. Your AP forwarder
# addresses (account.AP_FORWARDER_ADDRS) are deliberately NOT in this list —
# they're handled by Rule 4, which routes them to bucket 10 (Work FYI).
AUTOMATED_PATTERNS = [
    # ===== Generic local-part patterns — match the mailbox name anywhere =====
    # These are the dominant automation signal. They identify the
    # notification mailbox at any company, regardless of domain, without
    # burning humans at the same company.
    "notifications@", "no-reply@", "noreply@", "donotreply@",
    "do-not-reply@", "no_reply@", "team@", "hello@", "news@", "welcome@",
    "updates@", "alerts@", "digest@", "mailer@", "automated@",
    "info@e.", "messages-noreply", "drive-shares-noreply",
    "comments-noreply@docs.google.com", "calendar-notification@google.com",

    # ===== Specific noreply mailboxes at company-with-humans domains =====
    # IMPORTANT: never list the bare `@<company>.com` domain here. Use the
    # specific automated mailbox name. If a company has multiple automation
    # mailboxes (notifications@, no-reply@, billing@, alerts@, ...) the
    # generic local-part patterns above usually cover them. Add a line
    # below only when the company uses a non-standard automation mailbox
    # name that the generic list doesn't catch.
    "no-reply@zoom.us",
    "noreply@linear.app",        # not @linear.app — real humans work there
    "noreply@stripe.com",        # not stripe.com — many humans there
    "noreply@sanity.io",         # not sanity.io — founders + staff there
    "dse_NA1@docusign.net",      # docusign template-completion sender
    "dse@docusign.net",
    "@e.docusignmail.com",       # docusign's dedicated transactional subdomain
    "noreply@quickbooks.com",

    # ===== Dedicated notification subdomains (safe to whole-match) =====
    # These are transactional subdomains used only by email-service providers,
    # never by individual employees. They're not bare company domains.
    "@calendar.google.com",       # google calendar invitation bot only
]

# Publications / newsletters you read. These are automation senders, but they
# belong in Reading (bucket 9), not in-product notifications (bucket 8).
# Matched as a substring of the from-address, so a bare domain covers all of a
# publication's sending addresses (e.g. hello@ / newsletter@thebulletin.example).
# Add the newsletters and individual correspondents you want in Reading. These
# are examples — replace them.
READING_SENDERS = (
    "thebulletin.example",             # a subscribed publication (any sending address)
    "essays@indie-writer.example",     # an individual whose essays you read
)

def load_contacts() -> tuple[set[str], set[str], set[str], set[str], set[str], set[str], set[str]]:
    """Parse contacts.txt into 7 lists by category."""
    path = Path(__file__).parent.parent / "contacts.txt"
    vendors: set[str] = set()
    contractors: set[str] = set()
    trusted: set[str] = set()
    brokers: set[str] = set()
    keep_subscribed: set[str] = set()
    journalists: set[str] = set()
    org_fyi: set[str] = set()
    if not path.exists():
        return vendors, contractors, trusted, brokers, keep_subscribed, journalists, org_fyi
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        addr_field, _name, category = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
        # A single row may list several addresses for the same person,
        # comma-separated (e.g. their work and personal emails).
        addrs = {a.strip() for a in addr_field.split(",") if a.strip()}
        if category == "vendor":
            vendors.update(addrs)
        elif category == "contractor":
            contractors.update(addrs)
        elif category in ("trusted-warm", "personal-service"):
            # personal-service (e.g. a therapist or coach's scheduling mail) is
            # treated warm — always shown, never cold — but labeled separately.
            trusted.update(addrs)
        elif category == "broker":
            brokers.update(addrs)
        elif category == "keep-subscribed":
            keep_subscribed.update(addrs)
        elif category == "journalist":
            journalists.update(addrs)
        elif category == "org-fyi":
            org_fyi.update(addrs)
    return vendors, contractors, trusted, brokers, keep_subscribed, journalists, org_fyi


KNOWN_VENDOR_DOMAINS, CONTRACTOR_EMAILS, TRUSTED_WARM_EMAILS, BROKER_EMAILS, KEEP_SUBSCRIBED_EMAILS, JOURNALIST_EMAILS, ORG_FYI_SENDERS = load_contacts()

# Phishing patterns live in libs/email_review/src/email_review/phishing.py
# so smart-action and the classifier stay in sync. Import the shared
# has_phishing_tells here; do NOT duplicate the patterns.
from email_review.phishing import (
    has_phishing_tells as _shared_has_phishing_tells,  # noqa: E402
)

# Calendar invite subject pattern
CAL_SUBJ = re.compile(
    r"^(Accepted|Declined|Tentative|Updated invitation|Invitation|Canceled event|Tentatively Accepted)(\s+\w+){0,4}:",
    re.I,
)

# Decision-needed signals
DECISION_SIGNALS = [
    "save the date", "you're invited", "you are invited",
    "registration", "rsvp", "invitation to apply",
    "secure your ticket", "engagement agreement",
]

# Event-invite signals (route to bucket 6, not bucket 7).
# You're less likely to scan bucket 7 (marketing) but do want to scan event
# invites in case any are worth attending.
EVENT_SIGNALS = [
    "you're invited", "you are invited", "sign up for the event",
    "rsvp", "don't miss", "don’t miss", "save the date",
    "join us", "register now", " event ",
    "sign up now", "register here", "happy hour", "dinner with",
    "invitation from", "invitation to", "invite you to",
]
EVENT_MARKETING_SENDERS = [
    "luma-mail", "luma.com", "eventbrite", "open@greenhouse.com",
    "foundersbay", "calendly", "lu.ma",
]

def looks_like_event_invite(subj: str, snip: str, addr: str) -> bool:
    s = (subj or "").lower(); n = (snip or "").lower(); a = (addr or "").lower()
    if any(sig in s for sig in EVENT_SIGNALS) or any(sig in n[:300] for sig in EVENT_SIGNALS):
        return True
    if any(p in a for p in EVENT_MARKETING_SENDERS):
        return True
    return False

# Action-needed via AP signal
ACTION_AP_SUBJ = re.compile(r"\[DUE TODAY\]|\[ACTION REQUIRED\]|\[ACTION NEEDED\]", re.I)

# Investor updates from portfolio companies. If you're an angel/investor, these
# are informational — scan, no action expected → bucket 3 (FYI).
INVESTOR_UPDATE_SUBJ = re.compile(
    r"\binvestor\s+(update|letter|memo|report)\b",
    re.I,
)

# Notifications about an event you're *hosting* (vs being invited to). Luma
# sends "New waitlist entry for X", "X registered for your event", Eventbrite
# sends similar. These are work event-hosting notifications, distinct from
# event invites where you're the recipient.
HOSTING_NOTIFICATION_SUBJ = re.compile(
    r"\b(new waitlist entry|registered for|for your event|new attendee|"
    r"new (rsvp|ticket|signup|entry) for|new sign[- ]?ups? for)\b",
    re.I,
)

# Subjects that indicate an invoice/payment/contract process (drives the
# vendor-domain → bucket 10 routing). Personal asks from vendors that
# don't match this pattern fall through to normal classification.
INVOICE_SUBJ = re.compile(
    r"\b(invoice|bill\b|billing|payment|banking|receipt|reimbursement|"
    r"statement|wire|ACH|tax\b|W-?9)\b",
    re.I,
)

# Abbreviated invoice / PO tokens that appear in real vendor invoice
# subjects (e.g. "Acme Partners LP: #INV2100"). Used to identify legitimate
# AP-forwarded invoices so they aren't intercepted by the phishing INV/PO
# subject regex. Same shape as the phishing pattern, but used as a positive
# signal in the AP-forwarder context — we trust the AP forwarder address
# enough to treat an INV/PO token as a real invoice.
INVOICE_TOKEN_SUBJ = re.compile(
    r"#?(?:INV|PO|INVOICE|PURCHASE\s*ORDER)[-_\s]?#?\s*\d{3,}[\w\-]*",
    re.I,
)

# ---- Helpers ----

def parse_email(frm: str) -> str:
    if not frm: return ""
    m = re.search(r"<([^>]+)>", frm)
    if m: return m.group(1).lower()
    m = re.search(r"[\w\.\-\+]+@[\w\.\-]+", frm)
    return m.group(0).lower() if m else ""

def parse_display(frm: str) -> str:
    if "<" in frm:
        return frm.split("<")[0].strip().strip('"').strip()
    return ""

def domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if addr and "@" in addr else ""

def is_automated(addr: str) -> bool:
    return any(p in addr.lower() for p in AUTOMATED_PATTERNS) if addr else False

def is_known_vendor(addr: str) -> bool:
    d = domain(addr)
    return any(d.endswith(v) for v in KNOWN_VENDOR_DOMAINS)

def _in_contact_set(addr: str, contact_set: set[str]) -> bool:
    """True if addr (full email) or its bare domain is in contact_set.
    Supports both full email entries (foo@oreilly.com) and bare-domain
    entries (oreilly.com)."""
    if not addr:
        return False
    if addr in contact_set:
        return True
    d = addr.split("@", 1)[1].lower() if "@" in addr else ""
    return d in contact_set


def is_trusted_warm(addr: str) -> bool:
    """Check the trusted-warm list from contacts.txt."""
    return _in_contact_set(addr, TRUSTED_WARM_EMAILS)


def is_journalist(addr: str) -> bool:
    """Check the journalist (press-contact) list from contacts.txt."""
    return _in_contact_set(addr, JOURNALIST_EMAILS)


def is_warm_contact(addr: str) -> bool:
    """Senders you have marked as warm in contacts.txt — trusted-warm personal
    contacts plus journalists. Both are treated identically by the classifier
    today (exempt from cold detection, phishing, mute, and marketing
    heuristics); journalists are a distinct category only so press can be
    tracked and, in future, handled separately."""
    return is_trusted_warm(addr) or is_journalist(addr)


def is_org_fyi(addr: str) -> bool:
    """Senders whose mail is ALWAYS systematic org-process mail → bucket 10,
    regardless of subject. Unlike `vendor` (which only routes to bucket 10 on
    an invoice/payment subject so a vendor's *personal* email can still reach
    you), an `org-fyi` sender is a pure-automation administrator — e.g. a 401k
    provider — that never sends a personal note, so every message belongs in
    the Work FYI batch. Supports full-email and bare-domain entries."""
    return _in_contact_set(addr, ORG_FYI_SENDERS)


def is_org_contractor(addr: str) -> bool:
    """Personal-address contractors only (e.g. a contractor's gmail). Does NOT
    include your org-domain staff — staff personal messages need their own
    routing (bucket 3 FYI), not Work FYI (bucket 10 is for *automated /
    systematic* processes like vendor invoices, Docusign completions, calendar
    accepts)."""
    return addr in CONTRACTOR_EMAILS


def is_org_staff(addr: str) -> bool:
    """Returns True for any address on one of your org domains (any human staff
    member). Used to detect informational announcements vs personal Q&A from
    staff."""
    return domain(addr) in ORG_DOMAINS


def is_ap_forwarder(addr: str, frm: str = "") -> bool:
    return any(a in addr for a in AP_FORWARDER_ADDRS) or "via ap" in (frm or "").lower()


def is_known_good_sender(addr: str) -> bool:
    """Ground-truth-known-good senders: warm contacts (trusted-warm +
    journalists), known vendors, contractors, and org staff. These are exempt
    from the phishing heuristic (Rule 3).

    The shared phishing detector is intentionally broad on invoice/payment
    subjects (see email_review.phishing — `Invoice 1234 from Vendor` is a
    phishing tell by design). It relies on the classifier to exempt senders
    whose identity is already established here, exactly as it does for
    trusted-warm contacts. A real vendor invoice or a staff member's payment
    request with subject `Re: New payment request ... - invoice 1024` would
    otherwise be flagged as phishing before the vendor/finance routing rules
    ever run.

    The AP forwarder addresses are explicitly excluded: they're on your org
    domain but relay external content, so their identity is not trustworthy —
    fraud forwarded via AP must still hit phishing detection.

    Phishing protection for genuinely unknown senders (lookalike domains,
    display-name impersonation, wire-fraud body language) is unaffected:
    those addresses match none of these lists and still hit Rule 3."""
    if is_ap_forwarder(addr):
        return False
    return (
        is_warm_contact(addr)
        or is_known_vendor(addr)
        or is_org_contractor(addr)
        or is_org_staff(addr)
        or is_org_fyi(addr)
    )


INFORMATIONAL_PATTERNS = re.compile(
    r"\b(OOO|out of office|out next|off next|off (?:thurs|fri|mon|tues|wed)|"
    r"vacation|on leave|away from|heads[- ]?up|fyi|announcement)\b",
    re.I,
)

# Body opener that directly addresses you by first name, used to recognize that
# the sender is asking YOU even when the To: line targets someone else (a
# broker, a forwarder). Example: a reply To: someone else that opens "Alex —
# I'd be happy to chat..." is a direct ask. Built from your configured first
# name in account.py.
DIRECT_ADDRESS_PATTERNS = re.compile(
    r"^\s*(Hi|Hey|Hello|Dear|Hi there,?\s*)?\s*" + re.escape(ACCOUNT_FIRST_NAME) + r"\b[\s,!\.—–-]",
    re.I,
)

# One-line acknowledgments — thread is resolved, no action needed.
# Match against the first ~40 chars of the snippet (before any quoted prior
# message). Examples: "Wonderful! Erik", "Sounds good, thanks", "Got it!"
ACK_PATTERNS = re.compile(
    r"^\s*(wonderful|great|perfect|sounds good|got it|thanks?|thank you|"
    r"received|noted|will do|ok!?|okay!?|excellent|appreciated)[!\.,\s]",
    re.I,
)

# Your own addresses (from account.py) — used to recognize your outgoing mail.
OWN_ADDRS = set(ACCOUNT_ADDRS)


def has_phishing_tells(subj: str, frm: str, snip: str, addr: str) -> bool:
    """Thin wrapper around the shared phishing rules in
    email_review.phishing — keep the local signature classify.py's callers
    expect, delegate the actual logic so smart-action and the classifier
    never drift apart."""
    return _shared_has_phishing_tells(subj, frm, snip, addr)

# ---- The classifier ----

def classify(
    rec: dict[str, Any],
    cold_check: str,
    warm_names: dict[str, set[str]],
    brokered: set[str],
) -> tuple[str, str]:
    labels = set(rec["labels"])
    addr = rec["from_addr"]
    subj = rec["subject"] or ""
    snip = (rec["snippet"] or "").lower()
    frm = rec["from"]

    # Rule 1 (final, overrides everything): Your outgoing + still in inbox.
    # Two paths:
    #   (a) sent from one of your own addresses — Gmail tags it both SENT and
    #       INBOX when you're kept on the thread.
    #   (b) sent from a different own address and Cc'd back to you — only INBOX
    #       label is present, no SENT, but the From address tells us it's yours.
    #
    # EXCEPTION: calendar-invite messages from your own account are
    # auto-generated by Google Calendar, not real correspondence — when you
    # create or update an event, Gmail tags the system message SENT+INBOX, but
    # it's not "awaiting reply." Fall through to the calendar rule (Rule 5)
    # which routes these to bucket 8 alongside every other calendar
    # notification.
    if (addr in OWN_ADDRS and "INBOX" in labels) or ("SENT" in labels and "INBOX" in labels):
        if not CAL_SUBJ.match(subj):
            return ("5", "Your outgoing, awaiting reply")

    # Rule 2.5: AP-forwarded legitimate finance/process mail → bucket 10
    # BEFORE phishing detection. Real vendor invoices include `#INV####` /
    # `PO-####` tokens in the subject which match PHISHING_SUBJ_PAT and would
    # otherwise be flagged as phishing — this branch short-circuits to bucket
    # 10 for the AP-forwarder + legitimate-subject combination so real
    # invoices land in the Work FYI batch, not in marketing/spam.
    #
    # We deliberately do NOT short-circuit ALL AP-forwarder mail — spam
    # frequently arrives there too, and the phishing check still needs to
    # see it. Only mail with a recognized finance/process subject pattern is
    # promoted here; everything else continues through phishing detection
    # and ultimately lands in Rule 4 (AP catch-all → bucket 8).
    via_ap = is_ap_forwarder(addr, frm)
    if via_ap and (
        INVOICE_SUBJ.search(subj)
        or INVOICE_TOKEN_SUBJ.search(subj)
        or ACTION_AP_SUBJ.search(subj)
        or re.match(r"^(Approval needed:|Completed: Complete with Docusign|Re: Banking)", subj, re.I)
    ):
        return ("10", "AP-forwarded finance/process mail — Work FYI")

    # Rule 3: Phishing tells (override marketing/cold).
    # Skip the phishing check for known-good senders — trusted-warm contacts,
    # known vendors, contractors, and org staff. Their identity is already
    # established (contacts.txt / your org domain), so the broad invoice/payment
    # phishing heuristic must not misfire on their legitimate invoice subjects
    # (e.g. a real vendor invoice `... - invoice 1024`). Unknown senders still
    # get the full phishing check below.
    if not is_known_good_sender(addr) and has_phishing_tells(subj, frm, snip, addr):
        return ("7", "Phishing tells: impersonation / signature-spam subject / wire-fraud language")

    # Rule 3.5: Docusign — "needs signature" emails are TODOs.
    # "Completed:" notifications go to FYI / Work FYI since no action is needed.
    # Order matters: check Completed FIRST because the subject pattern
    # "Completed: Complete with Docusign: ..." also contains the substring
    # "Complete with Docusign" and would otherwise be misclassified.
    if "docusign" in addr:
        if subj.startswith("Completed:"):
            # Docusign completion notification — no action needed. Route to
            # bucket 10 if it came via the AP forwarder (team batch scan),
            # otherwise bucket 3 (general FYI).
            if is_ap_forwarder(addr, frm):
                return ("10", "Docusign completion via AP — Work FYI batch")
            return ("3", "Docusign completion notification — no action needed")
        # Any signing request → TODO. Docusign prefixes the subject with
        # "Complete with Docusign: ...", plus variants like
        # "Reminder: Complete with Docusign: ..." (a nudge to sign) and
        # "Corrected: Complete with Docusign: ..." (re-sent after a fix).
        # Match the "Complete with Docusign" stem anywhere so every prefix is
        # caught. These need your signature even when forwarded via AP.
        if "complete with docusign" in subj.lower():
            return ("4", "Docusign signing request — needs your signature")

    # Rule 4: AP-forwarder catch-all → bucket 8 (In-product notifications).
    # The finance/process branch ran earlier (Rule 2.5, before phishing). What
    # remains here is mail forwarded via AP that:
    #   - didn't match a finance/process subject pattern, AND
    #   - survived the phishing check (Rule 3) and Docusign check (Rule 3.5).
    # In practice this is SaaS product newsletters routed through AP, which
    # belong in In-product notifications. Real spam through AP would have
    # been caught by Rule 3 above; legitimate finance mail would have been
    # caught by Rule 2.5 above. Must run BEFORE the generic automation-sender
    # rule below.
    if via_ap:
        return ("8", "AP-forwarded SaaS product notification — in-product")

    # Rule 4.3: Acknowledgment-only replies → bucket 3 (FYI / thread resolved).
    # A short snippet that starts with "Wonderful!" / "Thanks!" / "Sounds
    # good" etc. means the sender is just acknowledging — no action required.
    # Match against the first chunk of the snippet, before any quoted prior
    # message starts ("On <date> X wrote:").
    snip_head = re.split(r"on .+ wrote:|on \w+, \w+ \d", snip or "", maxsplit=1)[0].strip()
    if snip_head and len(snip_head) < 80 and ACK_PATTERNS.match(snip_head):
        return ("3", "Acknowledgment-only reply — thread resolved, no action")

    # Rule 4.4: Investor updates from portfolio companies → bucket 3 (FYI).
    # These come from companies you've invested in and you want to scan but not
    # act on. Runs early so it overrides any phishing-tells / cold-detection
    # false positives.
    if INVESTOR_UPDATE_SUBJ.search(subj):
        return ("3", "Investor update from portfolio company — FYI")

    # Rule 4.5: Event-hosting notifications (Luma / Eventbrite / similar)
    # about an event you're hosting → bucket 10 (work-related event).
    # These need to run BEFORE the cold-detection branch (Rule 13) since the
    # event-host platforms send from no-reply addresses that look cold.
    if HOSTING_NOTIFICATION_SUBJ.search(subj):
        return ("10", "Event-hosting notification — work-related event")

    # Rule 5: Calendar invite notification.
    # Always systematic, and too spammy to scan in Work FYI, so route every
    # calendar invite/accept/decline/update to bucket 8 (in-product
    # notifications) regardless of sender.
    if CAL_SUBJ.match(subj):
        return ("8", "Calendar invite / update — in-product notification")

    # Rule 6a: Known contractor (personal address) or vendor → bucket 10 ONLY
    # when the subject indicates an invoice/payment/process thread. Personal
    # direct emails from vendors (e.g. a lawyer asking you to confirm
    # engagement details) must NOT auto-route to bucket 10 — they need to go
    # through normal classification so they land in bucket 1 (Reply needed)
    # when they ask you something.
    if (is_org_contractor(addr) or is_known_vendor(addr)) and INVOICE_SUBJ.search(subj):
        return ("10", "Vendor + invoice/payment-process subject → Work FYI")

    # Rule 6a.1: Always-FYI sender (pure-automation org administrator, e.g. a
    # 401k provider) → bucket 10 unconditionally. Unlike Rule 6a there's no
    # invoice-subject gate: these senders never write personal notes, so every
    # message (statements, contribution reports, plan notices) belongs in the
    # Work FYI batch rather than falling through to cold outreach.
    if is_org_fyi(addr):
        return ("10", "Always-FYI org administrator (e.g. 401k) → Work FYI")

    # Rule 6b: Org staff sending an informational broadcast → bucket 3 (FYI).
    # OOO heads-ups, "FYI" announcements, etc. — no action needed.
    # NOTE: bucket 10 is only for *automated / systematic* messages at the
    # company process level (vendor invoices, Docusign completions, calendar
    # accepts). Personal messages from staff go to bucket 3 instead, and direct
    # questions from staff fall through to bucket 1 via the normal classifier.
    if is_org_staff(addr) and INFORMATIONAL_PATTERNS.search(subj):
        return ("3", "Org staff informational broadcast (OOO / FYI / heads-up)")

    # Rule 7: Automation senders → in-product or reading.
    # EXCEPTION: warm contacts on automation domains (e.g. a real relationship
    # at a company whose SaaS-y domain is in AUTOMATED_PATTERNS) must NOT be
    # auto-routed to bucket 8. The contacts.txt entry is ground truth.
    # Reading publications: always Reading, whatever the sending address looks
    # like (every address on the publication's domain is newsletter content).
    if any(pub in addr for pub in READING_SENDERS):
        return ("9", "Subscribed publication / newsletter")

    if is_automated(addr) and not is_warm_contact(addr):
        if "digest" in addr:
            return ("9", "Subscribed digest / newsletter")
        return ("8", "Automation sender")

    # Rule 7.5: Incoming mail addressed to the AP forwarder group → Work FYI.
    # Catches first-time contractors / candidates submitting reimbursements
    # directly to AP so they don't fall into cold-outreach.
    #
    # Scoped to the AP forwarder group address(es) ONLY — deliberately NOT
    # individual teammates. They get cc'd on all kinds of non-finance mail, so
    # a cc to a person must never reclassify the thread as finance FYI.
    to_cc_addrs = set(re.findall(r"[\w\.\-\+]+@[\w\.\-]+", (rec.get("to", "") + " " + rec.get("cc", "")).lower()))
    if any(ap in to_cc_addrs for ap in AP_FORWARDER_ADDRS):
        return ("10", "Addressed to the AP forwarder — Work FYI")

    # Rule 8: Gmail Promotions category → marketing (UNLESS warm).
    # Warm contacts (per contacts.txt) override the Promotions category.
    trusted = is_warm_contact(addr) if addr else False

    if CATEGORY_PROMOTIONS in labels and not trusted:
        return ("7", "Gmail Promotions category")

    # Rule 10: Gmail Forums category → reading (mailing lists / newsletters).
    if CATEGORY_FORUMS in labels and not trusted:
        return ("9", "Gmail Forums category — mailing list / newsletter")

    # Rule 12.5: Genuine event invitation TO you. Calendar invites (Rule 5),
    # event-hosting notifications (Rule 4.5), AP/vendor/automation/marketing
    # senders (Rules 2.5–8) are all handled above, so anything left carrying
    # event-invite signals is a real invitation worth scanning → bucket 6
    # (event invites), ahead of cold-outreach and decision routing. This also
    # keeps an invite in event invites when a teammate is merely cc'd.
    if looks_like_event_invite(subj, snip, addr):
        return ("6", "Event invite — surfaced for scan")

    # Rule 13: Cold detection (after all overrides)
    if cold_check == "no prior outbound — candidate cold":
        # FP3 brokered relationship: appears as To: or Cc: on threads you sent
        if addr in brokered:
            pass  # treat as warm — fall through
        else:
            # FP1 same person different address
            disp = parse_display(frm).lower()
            if disp and disp in warm_names and addr not in warm_names[disp]:
                pass  # warm via same-name match
            else:
                return ("6", "No prior outbound to this sender")

    # Rule 14: Decision-needed signals
    if any(s in snip for s in DECISION_SIGNALS) or any(s in subj.lower() for s in DECISION_SIGNALS):
        return ("2", "Invitation / RSVP / decision language")

    # Rule 15: Gmail Updates / Social categories on non-warm senders →
    # in-product notifications.
    if (CATEGORY_UPDATES in labels or CATEGORY_SOCIAL in labels) and not trusted:
        return ("8", "Gmail Updates/Social category — in-product notification")

    # Default for warm humans: reply needed
    return ("1", "Warm sender — default to reply needed")

# ---- Main pipeline ----

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=100, help="messages to pull (default 100)")
    ap.add_argument("--offset", type=int, default=0, help="pagination offset (advance through the backlog)")
    ap.add_argument("--out", default="data/.apps/email-review/data.json")
    args = ap.parse_args()

    t0 = time.time()
    print(f"Pulling up to {args.n} inbox messages (offset {args.offset})...", file=sys.stderr)

    # Step 1: page through inbox to gather IDs.
    # Use labelIds=INBOX, NOT q=in:inbox: the `q=` search index is
    # eventually-consistent and can keep returning a thread for a while after
    # its INBOX label was removed (e.g. right after archiving, especially in
    # bulk), so just-archived mail would reappear on the next refresh. The
    # label index reflects modify calls immediately.
    ids: list[str] = []
    page_token = None
    while len(ids) < args.offset + args.n:
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages?labelIds=INBOX&maxResults=500"
        if page_token: url += f"&pageToken={page_token}"
        data = fetch(url)
        ids.extend(m["id"] for m in data.get("messages", []))
        page_token = data.get("nextPageToken")
        if not page_token: break
    ids = ids[args.offset : args.offset + args.n]
    print(f"  got {len(ids)} ids in {time.time()-t0:.1f}s", file=sys.stderr)

    # Step 2: fetch metadata for each
    def get_meta(mid: str) -> dict[str, Any]:
        return fetch(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
            f"?format=metadata"
            f"&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Cc"
            f"&metadataHeaders=Subject&metadataHeaders=Date"
        )

    t_meta = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        msgs = list(ex.map(get_meta, ids))
    print(f"  metadata in {time.time()-t_meta:.1f}s", file=sys.stderr)

    # Step 3: build records
    records = []
    senders_to_check = set()
    for m in msgs:
        labels = sorted(m.get("labelIds", []))
        hdrs = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        frm = hdrs.get("From", "")
        addr = parse_email(frm)
        rec = {
            "id": m.get("id"),
            "threadId": m.get("threadId"),
            "labels": labels,
            "from": frm,
            "from_addr": addr,
            "to": hdrs.get("To", ""),
            "cc": hdrs.get("Cc", ""),
            "subject": hdrs.get("Subject", ""),
            "date": hdrs.get("Date", ""),
            "snippet": html.unescape(m.get("snippet", "")),
        }
        records.append(rec)
        if addr and not is_automated(addr) and not is_org_contractor(addr) and not is_known_vendor(addr) and not is_org_fyi(addr):
            senders_to_check.add(addr)

    # Step 4: cold-outreach detection (sent-history check)
    def has_prior_sent(email: str) -> bool:
        q = urllib.parse.quote(f"in:sent to:{email}")
        d = fetch(f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults=1")
        return len(d.get("messages", [])) > 0

    def is_brokered(email: str) -> bool:
        q = urllib.parse.quote(f"in:sent (to:{email} OR cc:{email})")
        d = fetch(f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults=1")
        return len(d.get("messages", [])) > 0

    def is_owner_on_thread(thread_id: str) -> bool:
        """Stronger 'is this brokered?' check: pull the full Gmail thread and
        look for any message you sent inside it. Catches the case where you
        started the thread (asking for an intro) and the eventual reply is from
        someone you've never directly emailed.

        Example: you → a broker 'know a lawyer?' → broker forwards → the lawyer
        replies. The lawyer's reply by itself looks cold (no outbound history
        to them), but the thread itself has your original outgoing as the first
        message.
        """
        if not thread_id:
            return False
        t = fetch(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}"
            "?format=metadata&metadataHeaders=From"
        )
        for m in t.get("messages", []):
            for h in m.get("payload", {}).get("headers", []):
                if h["name"].lower() == "from":
                    if any(addr in h["value"].lower() for addr in OWN_ADDRS):
                        return True
        return False

    t_cold = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        history = dict(zip(senders_to_check, ex.map(has_prior_sent, senders_to_check)))
    cold_candidates = [e for e, w in history.items() if not w]
    with ThreadPoolExecutor(max_workers=12) as ex:
        brokered_results = dict(zip(cold_candidates, ex.map(is_brokered, cold_candidates)))
    brokered = {e for e, b in brokered_results.items() if b}

    # Thread-level check. For each cold-candidate message in the snapshot, see
    # if you are a sender anywhere on its full Gmail thread. If yes, mark that
    # record as "warm-via-thread."
    cold_records = [r for r in records if r["from_addr"] in cold_candidates and r["from_addr"] not in brokered]
    threads_to_check = list({r["threadId"] for r in cold_records if r.get("threadId")})
    with ThreadPoolExecutor(max_workers=12) as ex:
        thread_warmth = dict(zip(threads_to_check, ex.map(is_owner_on_thread, threads_to_check)))
    print(f"  cold/brokered/thread check in {time.time()-t_cold:.1f}s", file=sys.stderr)

    # Warm display-name map for FP1
    warm_names: dict[str, set[str]] = {}
    for r in records:
        if history.get(r["from_addr"]):
            d = parse_display(r["from"]).lower()
            if d:
                warm_names.setdefault(d, set()).add(r["from_addr"])

    # Step 5: classify
    for r in records:
        addr = r["from_addr"]
        if not addr:
            cold = "skipped (no sender)"
        elif is_automated(addr) or is_org_contractor(addr) or is_known_vendor(addr) or is_org_fyi(addr):
            cold = "skipped (automation/internal)"
        elif is_journalist(addr):
            cold = "warm (journalist via contacts.txt)"
        elif is_trusted_warm(addr):
            cold = "warm (trusted contact via contacts.txt)"
        elif history.get(addr) is True:
            cold = "warm (prior outbound)"
        elif addr in brokered:
            cold = "warm (brokered cc)"
        elif thread_warmth.get(r.get("threadId"), False):
            cold = "warm (you are a sender on this thread)"
        else:
            cold = "no prior outbound — candidate cold"
        r["cold_check"] = cold
        bucket, why = classify(r, cold, warm_names, brokered)
        r["final_bucket"] = bucket
        r["final_why"] = why

    # Step 5b: event-invite promotion. Anything in bucket 7 (marketing) that
    # looks like an event invite should be in bucket 6 instead, so you actually
    # see it when scanning. Bucket 7 is rarely opened.
    for r in records:
        if r["final_bucket"] != "7":
            continue
        if looks_like_event_invite(r["subject"], r["snippet"], r["from_addr"]):
            r["final_bucket"] = "6"
            r["final_why"] = "Event invite — surfaced for scan (would have been bucket 7 marketing)"

    # Step 6: thread-continuity — every message in a thread shares the
    # thread's "best" bucket. This catches the case where a warm contact
    # introduces a new person on a thread you received, and the new person's
    # reply (different sender, no prior outbound) would otherwise look cold.
    #
    # Priority: bucket 1 > 2 > 4 > 10 > 3 > 5 > 9 > 6 > 7 > 8.
    # The dominant bucket in a thread wins for everyone in it.
    PRIORITY = {b: i for i, b in enumerate(["1", "2", "4", "10", "3", "5", "9", "6", "7", "8"])}
    by_thread: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_thread.setdefault(r["threadId"], []).append(r)
    for thread_msgs in by_thread.values():
        if len(thread_msgs) < 2:
            continue
        best_bucket = min(thread_msgs, key=lambda r: PRIORITY.get(r["final_bucket"], 99))["final_bucket"]
        for r in thread_msgs:
            if r["final_bucket"] != best_bucket:
                r["final_why"] = f"thread-continuity: promoted to bucket {best_bucket} (was {r['final_bucket']}: {r['final_why']})"
                r["final_bucket"] = best_bucket

    # Step 6.5: bucket-1 "are you the actor?" check (runs after thread
    # continuity so a thread that became bucket 1 via promotion is also
    # checked). If the latest inbound on a bucket-1 thread doesn't have you in
    # To/Cc, the ask is for someone else on the thread.
    def addresses_in(header_val: str) -> set[str]:
        return {a.lower() for a in re.findall(r"[\w\.\-\+]+@[\w\.\-]+", header_val or "")}

    for thread_msgs in by_thread.values():
        if not any(r["final_bucket"] == "1" for r in thread_msgs):
            continue
        inbound = [r for r in thread_msgs if r["from_addr"] not in OWN_ADDRS]
        if not inbound:
            continue
        def is_broker_addr(a: str) -> bool:
            if a in BROKER_EMAILS:
                return True
            d = a.split("@", 1)[1] if "@" in a else ""
            return d in BROKER_EMAILS

        latest_inbound = max(inbound, key=lambda r: r["date"])
        to_only = addresses_in(latest_inbound["to"])
        cc_only = addresses_in(latest_inbound["cc"]) - to_only

        # Broker exception: if all of To: are brokers (the intermediary who
        # made the intro is on To by convention, not because the substance
        # is for them), treat you-on-Cc as the real recipient.
        #
        # BUT: if the broker is actively coordinating (2+ messages from the
        # broker in this thread), it's a brokered-scheduling pattern — the
        # broker is handling the back-and-forth on your behalf, so the ask is
        # for the broker, not you. Demote the whole thread to 3.
        broker_msg_counts: dict[str, int] = {}
        for r in thread_msgs:
            if is_broker_addr(r["from_addr"]):
                broker_msg_counts[r["from_addr"]] = broker_msg_counts.get(r["from_addr"], 0) + 1
        broker_is_coordinating = any(c >= 2 for c in broker_msg_counts.values())

        to_only_nonbroker = {a for a in to_only if not is_broker_addr(a)}
        if not to_only_nonbroker and (cc_only & OWN_ADDRS) and not broker_is_coordinating:
            # All To: addresses are brokers, and the broker isn't actively
            # coordinating — you are the actual audience. Do not demote.
            continue
        if broker_is_coordinating:
            # Broker is doing the work; you are just on cc for awareness.
            for r in thread_msgs:
                r["final_bucket"] = "3"
                r["final_why"] = (
                    f"Brokered scheduling — {max(broker_msg_counts, key=lambda k: broker_msg_counts[k])} "
                    f"is coordinating ({max(broker_msg_counts.values())} msgs in thread); "
                    f"you're on cc for awareness"
                )
            continue

        # Direct-address exception: if the body starts with "<your name> —" /
        # "Hi <your name>" / etc., the sender is addressing you directly even if
        # To: targets someone else. Body content beats header convention.
        snip = latest_inbound.get("snippet", "")
        if DIRECT_ADDRESS_PATTERNS.match(snip):
            continue

        if not ((to_only | cc_only) & OWN_ADDRS):
            # You aren't on either To: or Cc:
            for r in thread_msgs:
                r["final_bucket"] = "3"
                r["final_why"] = (
                    f"Latest inbound's To/Cc doesn't include you "
                    f"({latest_inbound['from'][:50]} → {latest_inbound['to'][:50]}) "
                    f"— ask is for another party"
                )
        elif (cc_only & OWN_ADDRS) and not (to_only & OWN_ADDRS):
            # You are on Cc only — the question is directed at the To
            # recipient(s). Classic "kept in the loop" pattern.
            for r in thread_msgs:
                r["final_bucket"] = "3"
                r["final_why"] = (
                    f"You are on Cc only; latest inbound's To: is "
                    f"{latest_inbound['to'][:60]} — ask is for the To recipient"
                )

    # Step 8: dedup bucket 5 by threadId (keep most-recent only) — still needed
    # for cases where the same outgoing fanned out to N recipients.
    seen_threads = set()
    deduped = []
    for r in records:
        if r["final_bucket"] == "5":
            if r["threadId"] in seen_threads:
                r["final_bucket"] = "_dup5"
                r["final_why"] = "duplicate of earlier bucket-5 thread"
            seen_threads.add(r["threadId"])
        deduped.append(r)
    records = [r for r in deduped if r["final_bucket"] != "_dup5"]

    # Fallback synthesis: every message gets at least a basic line so the
    # UI never shows just the subject. Hand-written overrides applied in
    # the self-review pass.
    for r in records:
        if r.get("synthesis"):
            continue
        sender = re.sub(r"\s*<.*", "", r.get("from", "")).strip().strip('"')
        if not sender:
            sender = r.get("from_addr", "")
        subj = re.sub(r"^(Re:\s*|Fwd:\s*)+", "", r.get("subject", ""), flags=re.I)
        r["synthesis"] = f"{sender} — {subj}" if sender else subj

    # Output
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "stats": {
            "total": len(records),
            "requested": args.n,
            "offset": args.offset,
            "elapsed_sec": round(time.time() - t0, 1),
            "unique_senders_checked": len(senders_to_check),
            "warm_senders": sum(1 for v in history.values() if v),
            "cold_candidates": len(cold_candidates),
            "brokered_warm": len(brokered),
        },
        "messages": records,
    }, indent=2))
    print(f"Wrote {out_path} ({len(records)} records, {time.time()-t0:.1f}s total)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
