# Email digest rules

This is the canonical rules document for the email digest. The `email-digest`
skill (`.agents/skills/email-digest/SKILL.md`) reads this file on every run and
treats it as the source of truth — when this document and the skill disagree,
this document wins. Edit this file directly to change classifier behavior.

> These rules are **opinionated** — they encode one person's taxonomy and
> heuristics. Treat them as a starting point and tune the buckets, senders, and
> thresholds to your own inbox.

## Scope: what counts as "inbox"

Treat Gmail archive as "done." Any message with the `INBOX` label is in scope;
anything archived (no `INBOX` label) is out. The digest reads the *entire*
unarchived inbox, not just the most recent N. If something matters and hasn't
been archived, it must be classified.

### A quirk to know about

**Sent messages can also be in INBOX.** When you're kept on a thread you sent,
Gmail may show a message with both `SENT` and `INBOX` labels, so it surfaces
alongside inbound mail. Any message with both `SENT` and `INBOX` labels →
bucket 5 (Sent / awaiting reply), no other classification needed. This rule
overrides everything else. The classifier also recognizes your own outgoing
mail by the From address (set your addresses in `account.py`).

## The 10 buckets

The digest classifies every inbox message into exactly one of these.

### 1. Reply needed (warm)

A person you have emailed before is asking you *specifically* a question you can
answer in under ~10 min, without it becoming a TODO or requiring real
evaluation.

- Must be a direct ask of you, not someone cc'ing you for visibility while
  another party handles the actual reply.
- Prior outbound history with the sender is a prerequisite. If absent, it
  goes to bucket 6 (Cold outreach), not here.
- 48-hr SLA: anything older than 48hrs in this bucket should be marked
  "overdue" in the digest.

Examples:
- A colleague asking "are you free for dinner after the talk on the 19th?"
- Someone answering "Tuesday or Wednesday should work" — needs a yes/no
- A friend asking for an intro to someone at your company

NOT in this bucket:
- A broker handling scheduling on your behalf → bucket 3 (FYI). You are To: but
  the broker is responding.
- A teammate asking about something another teammate is handling → bucket 3.

### 2. Decision needed

Invitations, opportunities, RSVPs, and asks that require you to *evaluate and
decide* (yes / no / not now). Distinct from bucket 1 because the work is
*deciding*, not *replying with a known answer*. Distinct from bucket 4 (TODO)
because the work is short — a decision, not a project.

Examples:
- A reunion save-the-date — do you want to go?
- A retreat registration — yes/no
- A dinner invite for a specific date — yes/no
- A sales call request — take the call or not?
- An engagement agreement — proceed as a client or not?

### 3. FYI / read

You're cc'd or otherwise looped in, but nobody is asking you a question.
May still want to skim.

Includes:
- Threads where an intermediary is handling the back-and-forth and you're being
  kept in the loop — even if you're on the To: line.
- Save-the-date or save-the-info threads that don't require a decision now.
- Internal threads between two colleagues where you're cc'd.

### 4. TODO

Real work item that goes onto your plate. Not a quick reply, not a quick
decision. Eventually routes into the GTD system at `runtime/gtd/`.

Examples:
- A logistics letter for a workshop you're running — you need to act on it.
- A forwarded request saying "can you handle this".
- **Docusign signing requests** — any Docusign email whose subject contains
  "Complete with Docusign" needs your signature, so it goes here. This covers
  the bare request, `Reminder: Complete with Docusign: ...` nudges, and
  `Corrected: Complete with Docusign: ...` re-sends — even when the document is
  forwarded via your AP forwarder (AP can't sign for you). Docusign
  `Completed:` notifications are the exception: nothing is left to do, so they
  stay in bucket 3 (FYI), or bucket 10 if forwarded via AP.

### 5. Sent / awaiting reply

Your outgoing messages that are still unanswered. Detected when a thread carries
both `SENT` and `INBOX`, or when the From address is one of your own and the
message is still in INBOX.

Rules:
- Dedup by `threadId` — collapse "same message to N recipients" into one.
- Sort by age descending. Oldest unanswered are highest priority.

### 6. Cold outreach (+ event invites)

A person you have *never* emailed before, asking you something — **plus event
invites of any kind, even from list-marketing senders.**

Keep this bucket browsable: cold outreach gets scanned for missed warm
contacts, and event invites get scanned because you might actually want to
attend some of them. You're less likely to look inside bucket 7
(marketing/spam), so event invites must NOT land there.

Event-invite signals (route here, not to bucket 7):
- Subject contains "you're invited", "sign up", "RSVP", "don't miss",
  "save the date", "join us", "event", or an upcoming date
- From an event-marketing list (Luma, Eventbrite, Greenhouse events,
  similar) but the content describes a specific event

**Synthesis for event invites must include WHAT the event is and roughly
WHEN.** Examples:
- `Greenhouse — "Open for Ops" recruiting-ops event (date in body)`
- `Event platform — "Sign up for the event tomorrow" — what event?`

See "Cold-outreach detection" below for the cold-outreach methodology and
false-positive checks.

### 7. Marketing / promo / spam / phishing

Automated marketing, sales blasts, solicitations from list-based senders, and
phishing/spoof attempts. Distinct from cold outreach (bucket 6): this bucket is
automated or impersonal, not a human reaching out personally. Gmail's
`CATEGORY_PROMOTIONS` label is a strong starting signal.

Phishing tells (route here, not bucket 6):
- Sender display name impersonates you or a known contact
- Body requests urgent ACH/wire/payment
- Subject is a signature-spam pattern (e.g. "Re: Shareholders Consent<hex>")

TLD alone is NOT a phishing tell. Legitimate senders use `.xyz`, `.info`,
`.shop`, etc. Route a bare cold pitch from one of these TLDs to bucket 6 (Cold
outreach) unless it has another phishing tell.

### 8. In-product notifications

Calendar invite accepts/declines, payment-lifecycle notices, SaaS comment
notifications (docs, issue trackers, etc.). Gmail's `CATEGORY_UPDATES` and
`CATEGORY_SOCIAL` labels are strong signals. Most of these should be turned off
at the source. The digest produces a "settings to tweak" sub-list when it sees
notifications from configurable sources.

### 9. Reading

Newsletter subscriptions, community digests, friend essays forwarded for
enjoyment. Things you chose to receive for enjoyment or learning. Gmail's
`CATEGORY_FORUMS` label is a signal, as is any sender in `READING_SENDERS`.

The smart-action button is enabled here in addition to archive — some "reading"
items turn out to be unwanted spammy content. Smart action tries unsubscribe
first (via the `List-Unsubscribe` header), and falls back to mute or archive.
The save-for-later button stays the primary CTA for items worth coming back to.

### 10. Work FYI

**Automated / systematic things at the company process level.** You want these
as a separate batch to scan (different cadence than personal FYI). This bucket
is NOT "anything from your org" — personal messages from colleagues (OOO
heads-ups, casual updates, direct questions) go to bucket 3 (FYI) or bucket 1
(Reply needed), not here.

What belongs in bucket 10:
- Vendor invoices and payment-info threads (senders on your `vendor` list).
- Contractor invoices on personal addresses (`contractor` list).
- AP forwarder mail (your `AP_FORWARDER_ADDRS`) — bill-pay lifecycle, approval
  requests, receipts.
- Calendar invite accepts/declines auto-generated by the calendar system.
- Docusign completion notifications via AP.

What does NOT belong in bucket 10:
- A colleague's "OOO time ahead" personal heads-up → bucket 3 (FYI)
- A colleague replying to a thread with a question → bucket 1 (Reply needed)
- Internal team announcements → bucket 3
- **A vendor or contractor sending you a personal direct email** (e.g. a lawyer
  asking you to confirm engagement details) → bucket 1 (Reply needed). The
  classifier only routes vendor mail to bucket 10 when the subject indicates an
  invoice / payment / banking / billing / receipt / reimbursement process —
  personal asks fall through to normal classification.

**Action-tagged mail via the AP forwarder also belongs here.** Even
`[DUE TODAY]` / `[ACTION REQUIRED]` / `Approval needed:` items arriving via the
AP forwarder are usually for someone else on your team to handle — not you. You
want visibility but not to be the actor, so these stay in bucket 10 instead of
getting promoted to bucket 4 (TODO). The bucket 4 bar for AP-forwarded mail is
"you are specifically and uniquely the one who must do the work" — which is
rare, since AP exists precisely to handle these.

## Layer 1: label-based pre-filter

Apply these in order. First match wins. This gives a starting bucket; the
content-aware pass (next section) can override.

| Signal | Starting bucket |
|---|---|
| Has both `SENT` and `INBOX` labels (your outgoing kept visible) | 5 (final — no further classification) |
| Gmail `CATEGORY_PROMOTIONS` | 7 (content pass may re-route to 6 if cold outreach) |
| Gmail `CATEGORY_SOCIAL` or `CATEGORY_UPDATES` | 8 |
| Gmail `CATEGORY_FORUMS` | 9 |
| Sender matches known-automation pattern | 8 |
| Subject matches `^(Accepted\|Declined\|Updated invitation\|Invitation\|Tentatively Accepted):` | 8 |
| Sender domain is on known-vendor / contractor / accounting list | 10 |
| None of the above | ambiguous — content pass decides |

### Known-automation sender patterns

Extend as new ones are found. Prefer local-part patterns (`noreply@`) over
whole domains — see the detailed rationale in `scripts/classify.py`.

```
notifications@*
no-reply@*, noreply@*, donotreply@*
*@calendar.google.com, calendar-notification@google.com
"via AP" in display name
comments-noreply@docs.google.com
drive-shares-noreply@google.com
messages-noreply@*
*@docusign
noreply@linear.app
digest@*
```

Your AP forwarder addresses (`account.AP_FORWARDER_ADDRS`) are NOT in this list
— they're routed to bucket 10 (Work FYI) by a dedicated rule.

### Known vendor / contractor / accounting domains

Maintained in `contacts.txt` under the `vendor` / `contractor` / `org-fyi`
categories. Extend as new ones appear.

## Layer 2: content-aware rules

Read each message's snippet (and full body when the snippet is inconclusive).
For each, answer one question per bucket in order. First "yes" wins.

### Reply needed? (→ 1)

- Prior outbound history with sender exists, AND
- Sender is asking you a question or decision, AND
- The ask is directed at you personally (not "Lauren, can you confirm…" with
  you on cc), AND
- No other internal owner is already responding in-thread.

**Mandatory verification**: every bucket-1 candidate must pass the "are you the
actor" check before being shown. Read the *latest inbound message* in the
thread (the most recent message not from you) and check:

1. **Are you in the `To:` or `Cc:` of the latest inbound?** If not, the ask is
   for someone else on the thread. Demote to bucket 3 (FYI). The classifier
   encodes this in code.

2. **Is the latest inbound the sender taking action on their side?** Phrases
   like "I'll ask our program director", "I will get back to you", "let me
   check and confirm" mean the ball is in the sender's court, not yours. Demote
   to bucket 3 (FYI). This requires content reading and should happen in the
   manual self-review pass, not in code.

3. **Is the latest inbound an answer that closes your loop?** If you asked a
   question and the latest reply provides the answer with no follow-up
   question, the thread is resolved. Demote to bucket 3 or bucket 9.

**Conservative default:** if you're not sure, keep in bucket 1. False
retentions waste a few seconds of scanning; false demotions hide items you'd
want to see.

If all yes → bucket 1.

### Decision needed? (→ 2)

- Sender is presenting an invitation, opportunity, RSVP, or yes/no ask, AND
- You have not already accepted/declined elsewhere (check thread history), AND
- The decision is short — yes/no/not-now — not a project.

If yes → bucket 2.

### Work FYI? (→ 10)

- Sender is on one of your org domains OR matches the vendor / contractor /
  org-fyi list, AND
- Thread is about invoices, payments, banking info, contracts, or vendor
  logistics, AND
- Your AP forwarder or a teammate is the primary responder (not you).

If yes → bucket 10.

### FYI / read? (→ 3)

- You are cc'd (not on To:), OR
- You're on To: but the question is being handled by someone else, OR
- The thread is between two warm contacts and has resolved itself.

If yes → bucket 3.

### TODO? (→ 4)

- Explicitly asks for non-trivial work (>15 min, drafting / reviewing /
  multi-step), OR
- Is a forwarded request saying "can you handle this".

If yes → bucket 4.

### Reading? (→ 9)

- Sender is a subscription you opted into, OR
- Has the `CATEGORY_FORUMS` label, OR
- Is informational rather than reply-needed.

If yes → bucket 9.

### In-product notifications? (→ 8)

- Sender matches known-automation patterns, OR
- Subject is a calendar invite accept/decline/update, OR
- Has the `CATEGORY_UPDATES` / `CATEGORY_SOCIAL` label (SaaS notification,
  payment lifecycle, comment notification, etc.).

Also flag for the weekly "settings to tweak" sub-list if it's a known-
configurable source.

If yes → bucket 8.

### Marketing / spam / phishing? (→ 7)

- Automated marketing blasts / `CATEGORY_PROMOTIONS`, OR
- Sender display name impersonates someone known → phishing, OR
- Sales pitch from a list-based sender with no prior history, OR
- Claims urgent ACH/wire payment → phishing.

If yes → bucket 7.

### Cold outreach (→ 6)

Anything left that's a human sender with no prior outbound history, after the
false-positive checks below.

## Cold-outreach detection: methodology

### Step 1: Extract unique senders

Parse the `From:` header on every inbox message. Normalize to lowercase email
address. Build a map `email -> [messages from this sender]`.

### Step 2: Skip automation

Drop any sender whose email matches the known-automation patterns. These are
never "cold" — they're bucket 7 or 8.

### Step 3: Check Gmail sent history

For each remaining sender, query:

```
GET https://gmail.googleapis.com/gmail/v1/users/me/messages
    ?q=in:sent+to:<sender_email>&maxResults=1
```

- Results > 0 → sender is **warm**.
- Results == 0 → sender is **candidate cold**, proceed to step 4.

Parallelize across senders (~12 workers).

### Step 4: False-positive checks on candidate cold

For each candidate, run these checks. If ANY matches, the sender is NOT cold —
route to the bucket noted.

#### FP1: Same person, different address

Normalize display name from the `From:` header (lowercase, strip extra
whitespace). If a different email exists in the warm list with the same display
name → mark as warm. (Handles the "same person on a personal gmail" case.)

#### FP2: Org domain or known vendor/contractor

If sender domain is one of your org domains or matches the vendor/contractor
list → route to bucket 10 (Work FYI).

#### FP3: Brokered relationship

Check if this sender's email appears as To: or Cc: on any message you have sent:

```
GET .../messages?q=in:sent+(to:<sender>+OR+cc:<sender>)&maxResults=1
```

If yes → mark as warm-via-broker; route as if warm.

#### FP4: Missed automation

Re-check against an extended automation list. If matches → route to bucket 8
(In-product) or 9 (Reading) as appropriate. Extend the automation list with any
new patterns found.

#### FP5: Phishing tells

- Display name impersonates you or a known contact
- Body requests ACH/wire payment urgently
- Subject is a signature-spam pattern (`Re: Shareholders Consent<hex>` etc.)

Route to bucket 7 (Marketing/spam/phishing), not bucket 6.

TLD alone is NOT a phishing tell. A cold pitch from a spam-farm TLD with no
other phishing tells belongs in bucket 6, not 7.

### Step 4.5: Thread continuity

Before finalizing a "cold" verdict, check whether this message shares a
`threadId` with another inbox message whose sender IS warm. If so, the two
messages are part of the same conversation — the cold sender is warm-by-
introduction. Promote to whatever bucket the warm-sender message landed in.

*Example:* a warm contact introduces a new person on a thread you received. The
new person's reply arrives with no prior outbound from you, so the sent-history
check returns cold. Thread continuity catches it: the intro message is bucket 1,
so the reply is also bucket 1.

**This rule applies more broadly than cold detection** — any time two messages
share a `threadId`, they should land in the same bucket. The classifier picks
the highest-priority bucket and promotes the others. Priority order:
1 > 2 > 4 > 10 > 3 > 5 > 9 > 6 > 7 > 8.

### Step 5: What's left is genuinely cold

Surface in bucket 6. **Do not auto-discard.** The digest should show sender
name/email, subject, a one-line summary, and a suggested action (ignore / mute
/ mark as spam / reply).

## Edge cases and known gaps

- **Display-name normalization for FP1 is naive.** Nicknames, reversed names,
  and accents will trip it. Iterate as misses are found.
- **The known-vendor and known-automation lists must be maintained.** Every
  digest run should surface new senders that hit "ambiguous" so the lists can
  be extended.
- **48-hr SLA threshold for bucket 1** is configurable.
- **Bucket 5 recipient fan-out:** when the same outgoing was sent to N
  recipients, dedup by `threadId` to one entry with a list of who hasn't
  replied.
- **Decision needed (bucket 2) vs Reply needed (bucket 1):** the boundary is
  "do I know the answer?" If yes and short → 1. If it needs thought, weighing,
  or saying yes/no to an invite → 2.

## Data sources

| Source | API | Notes |
|---|---|---|
| Gmail | `latchkey curl https://gmail.googleapis.com/gmail/v1/...` | Service `google-gmail`. Read builds the digest; write performs the one-click actions (archive, label/mute, spam, unsubscribe). |
| Slack | `latchkey curl https://slack.com/api/...` | Service `slack`. Optional — use for DMs and mentions; not yet integrated into the digest. |
