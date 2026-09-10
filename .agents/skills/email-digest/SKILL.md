---
name: email-digest
description: Build an inbox digest for the user. Reads recent Gmail, classifies every message into the 10-bucket taxonomy via Gmail label hints + content-aware rules, and surfaces what needs a reply within 48hrs, what decisions are pending, what's truly cold outreach, what you're waiting on, and what's pure noise. Use when the user says "show me my digest", "what's in my inbox", "what do I need to reply to", "what am I waiting on", or asks to triage / process their email.
---

# Email digest skill

## Always start here

Before doing anything, read `.agents/skills/email-digest/RULES.md`. It is the
canonical, user-editable source of truth for the 10-bucket taxonomy, the
label-based pre-filter, the content-aware rules per bucket, the cold-outreach
detection methodology, and the maintained lists of known automation patterns
and vendor domains. When this file and RULES.md disagree, RULES.md wins.

Set your identity in `system/apps/email_review/src/email_review/account.py` (your
name, addresses, org domains, AP forwarder) and fill in
`.agents/skills/email-digest/contacts.txt` (your who's-who allowlist) before
the first run — the classifier reads both.

## Pipeline (every digest run)

1. **Pull.** Fetch *everything currently in the inbox* — not just the most
   recent N. Treat "archive" as "done," so any unarchived message is in scope
   for the digest. In Gmail terms this means anything with the `INBOX` label.
   Page through the full set with `labelIds=INBOX` and `maxResults=500` until
   `nextPageToken` is absent. Use `format=metadata` + headers
   `From,To,Cc,Subject,Date`.

   **Performance:** parallelize the metadata fetches (12 workers). For
   day-to-day digest runs, restrict to a recent window to keep latency low; do
   the full sweep weekly or when explicitly asked. The `classify.py` script
   implements this pull.

2. **Pre-filter by label.** Apply the table in RULES.md "Layer 1." First match
   wins; result is a *starting bucket*. Gmail's native category labels
   (`CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_SOCIAL`,
   `CATEGORY_FORUMS`) are the primary label signals — no external mail client
   required. Anything that hits "None of the above" is ambiguous and MUST go
   through the content pass.

3. **Cold-outreach detection.** Run the methodology in RULES.md "Cold-outreach
   detection" for every sender on every message whose starting bucket is
   1 / 2 / 3. The false-positive checks (FP1-FP5) re-route wrongly-flagged-cold
   items to buckets 3, 7, 8, 9, or 10.

4. **Content-aware pass.** Read snippet (and full body when inconclusive) for
   every message and apply the bucket-question order in RULES.md "Layer 2."
   Override the pre-filter bucket whenever the content disagrees. Labels alone
   are ~80% accurate; the remaining 20% are exactly the items that matter most.

5. **Self-review and fix (REQUIRED).** Before rendering anything, scan every
   bucket's items yourself looking for obvious misclassifications, e.g.:
   - Senders matching obvious automation patterns the list didn't catch.
   - Phishing tells (hex-string-in-subject signature requests, display-name
     impersonation, urgent-wire body language) in cold outreach instead of
     marketing/spam. TLD alone is NOT a phishing tell.
   - Action-needed items via the AP forwarder (`[DUE TODAY]`,
     `Approval needed:`) routed to bucket 4 instead of bucket 10.
   - **Event invites routed to bucket 7 (marketing) — they belong in bucket 6
     (Cold outreach + event invites)** so you actually see them.
   - **Same-thread messages in different buckets — apply thread continuity.**
     If two messages share a `threadId` they must share a bucket.
   - **Bucket 1 items where you aren't actually the actor.** For every bucket-1
     thread, read the latest inbound message and verify you are being directly
     asked (see RULES.md "Reply needed?"). Conservative default: if unsure,
     keep in bucket 1.

   Apply every fix you'd note. Encode the fix as either a code change or an
   addition to the maintained lists in RULES.md. Re-run and verify against
   specific cases (not just "rule implemented"). Only then proceed.

   **Synthesis quality bar.** Every item gets a one-line synthesis following
   the pattern `{Sender (optional org)} — {topic} {optional context}`. For
   event invites, **include WHAT the event is and roughly WHEN** so you can
   scan and decide.

6. **Dedup bucket 5.** Group by `threadId`. Collapse "same outgoing message to
   N recipients" into one entry with a list of who hasn't replied. Sort by age
   descending.

7. **Render.** The digest is rendered by the `email-review` app (a
   FastAPI app; see `system/apps/email_review`). Run `classify.py` to write
   `data/.apps/email-review/data.json`, then the app reads it and renders
   the bucket-grouped triage UI. The Refresh / Categorize buttons in that UI
   re-run this pipeline.

## One-click actions (the email-review UI)

The email-review web UI performs the triage actions directly against Gmail via
`latchkey curl` — no external mail client. For each thread you can:

- **Archive** — remove the `INBOX` label.
- **Mark spam** — add `SPAM`, remove `INBOX`.
- **Mute** — apply the app's `Muted` Gmail label (created on first use) and
  remove `INBOX`. Future messages on a muted thread are re-archived on the next
  refresh by `scripts/propagate_mutes.py`.
- **Unsubscribe** — one-click via the message's `List-Unsubscribe` header
  (RFC 8058 POST, GET, or mailto).

### Smart-action ruleset (buckets 6 and 7 buttons)

The UI shows an "Archive / Unsub / Mute / Spam" button on buckets 6 (Cold
outreach + events) and 7 (Marketing / spam / phishing), and per-row on 8/9.
When you click it, the service decides per-thread which action to take. Decision
order (implemented in `gmail_actions.smart_action`):

1. **Keep-subscribed override.** If the sender is on the `keep-subscribed`
   list (contacts.txt), archive only — never unsubscribe.
2. **Phishing in bucket 7 → mark spam.** Never click a phisher's unsubscribe
   link.
3. **Internal-forwarder guard.** If the `List-Unsubscribe` header points back
   at one of your own org domains (`account.ORG_DOMAINS`), archive only —
   unsubscribing would drop you from your own group. Preflight standalone with
   `scripts/check_unsub_target.py --thread-id <id>` (verdict `external` = safe,
   `internal-forwarder` / `no-header` = do not unsubscribe).
4. **Unsubscribe** via the `List-Unsubscribe` header if present and usable.
5. **Mute** for buckets 6/7/8/9 when there's no usable unsubscribe option —
   applies the `Muted` label and removes INBOX.
6. **Mark as spam** if the snippet has phishing tells (urgent ACH/wire).
7. **Archive** otherwise.

Every action returns enough info for the undo toast to reverse it. Unsubscribe
undo can re-add INBOX but cannot re-subscribe.

## Maintaining the contacts file

`.agents/skills/email-digest/contacts.txt` is the persistent home for "who's
who" information — vendors, contractors, friends, brokers, and addresses that
should always route a certain way. The classifier reads it on every run.

**Whenever the user mentions a contact, vendor, or relationship — explicitly or
in passing — update this file before responding.** Format: tab-separated
`email-or-domain  TAB  name  TAB  category  TAB  notes`. The categories and
their routing are documented at the top of `contacts.txt`; the classifier
routes:
- `vendor` and `contractor` → bucket 10 (Work FYI) on invoice/payment subjects
- `org-fyi` → bucket 10 unconditionally
- `trusted-warm` / `personal-service` / `journalist` → treated warm
- `broker` → catches the "X introduces people to you" pattern (FP3)
- `keep-subscribed` → never auto-unsubscribed by smart-action

### Bulk archive a sender (with exception list): `bulk_archive.py`

When the user says "clear out the X@Y.com emails from my inbox, but leave
anything that needs my eyes," use the bulk archive flow:

1. **Surface candidates.** Pull every message matching the query and run an
   LLM-judge pass (or a content filter) to identify the small set that needs
   review. Save those to a `look_list.json` file.

2. **Dry-run.** Show the user the candidate count and the look-list:

   ```bash
   uv run python .agents/skills/email-digest/scripts/bulk_archive.py \
       --query "from:notifications@example.com in:inbox" \
       --keep-from /path/to/look_list.json
   ```

   This writes `runtime/bulk_archive/pending.json` with every would-archive ID
   and prints a summary. Always confirm before passing `--yes`.

3. **Archive.** Re-run with `--yes`. The script writes
   `runtime/bulk_archive/last_run_<ts>.json` containing every archived ID, so
   the action is reversible.

4. **Undo if needed.**

   ```bash
   uv run python .agents/skills/email-digest/scripts/bulk_archive_undo.py last_run_<ts>.json
   ```

The query is just a Gmail search string — works for any sender, label, or
filter combination.

### Bulk audit: `contact_audit.py`

For a periodic refresh (run monthly), use the audit script:

```bash
# Dry-run: scan last 180 days of sent mail, LLM-judge candidates,
# write a review file to runtime/contact_audit/proposed_contacts_<date>.md
uv run python .agents/skills/email-digest/scripts/contact_audit.py

# Same but actually append to contacts.txt under a dated section
uv run python .agents/skills/email-digest/scripts/contact_audit.py --write
```

The script pulls your sent mail via Gmail API, filters out automated senders +
existing contacts + your own aliases (read from `account.ACCOUNT_ADDRS`), tiers
by frequency (3+ auto-included, 1-2 LLM-judged), and adds back mass-invite
recipients who didn't reply but are known contacts. Default output is a review
markdown file — always show that to the user before passing `--write`.

## When to update RULES.md

- A new automation sender pattern slips through → add to the list in RULES.md.
- A new vendor/contractor domain appears → add it to contacts.txt.
- A misclassification pattern surfaces in conversation → encode the rule in
  RULES.md, not in this file.
- The 10-bucket taxonomy changes → update RULES.md first, then this skill.

## Open work

- Slack integration not yet built. Use `latchkey services info slack` to
  confirm scope, then add a Slack pull step before the digest render.
- Reply compose is intentionally not part of this app — it's read-and-triage
  only. An adopter could add plain Gmail drafts if desired.
