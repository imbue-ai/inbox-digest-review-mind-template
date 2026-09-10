"""Adopter-configured account identity.

Edit these constants to match your own inbox before running the digest. They
are the single source of truth for "who am I" across the classifier
(`classify.py`), the phishing detector (`phishing.py`), and the one-click
Gmail actions (`gmail_actions.py`), so the three never drift apart.

Everything here ships with obvious placeholder values — the app runs, but you
should replace them with your real name, addresses, and organization domain(s)
so the classifier recognizes your own mail and the phishing guard can spot
someone impersonating you.
"""

from __future__ import annotations

# Your display name, exactly as it appears on mail you send. Used to catch
# display-name impersonation and body name-drops in phishing detection, and to
# recognize when a reply addresses you directly ("Hi Alex, ...").
ACCOUNT_NAME = "Alex Doe"

# Just the first name, derived from ACCOUNT_NAME. Used by the "is this reply
# addressed to me?" check.
ACCOUNT_FIRST_NAME = ACCOUNT_NAME.split()[0]

# Every email address you send from (work, personal, aliases). The classifier
# uses this to recognize your own outgoing mail and to exempt you from the
# cold/phishing checks.
ACCOUNT_ADDRS: set[str] = {
    "alex@yourcompany.example",
    "alex.doe@gmail.example",
}

# Domains your organization owns. Two uses:
#   1. A List-Unsubscribe link pointing back at one of these is an internal
#      forwarder / group — the app refuses to "unsubscribe" you from your own
#      org (which would silently drop you from a company mailing list).
#   2. Mail from a human on one of these domains is treated as internal staff.
ORG_DOMAINS: tuple[str, ...] = ("yourcompany.example",)

# Shared accounts-payable / forwarder addresses that relay external mail
# (invoices, receipts) into your inbox. Leave this empty if you don't use one.
# Their From identity says nothing about the original sender, so they stay
# subject to phishing detection.
AP_FORWARDER_ADDRS: tuple[str, ...] = ("ap@yourcompany.example",)
