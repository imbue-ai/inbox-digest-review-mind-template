---
title: Inbox Digest & Review
description: A Gmail inbox digest and one-click triage app: a skill classifies recent mail into actionable buckets, and a web view lets you read and clear each item (archive / spam / unsubscribe / mute).
thumbnail: inspiration-inbox-digest-review.svg
---

# Inbox Digest & Review

This file is the manifest for the **Inbox Digest & Review** inspiration (slug:
`inbox-digest-review`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A Gmail inbox digest and one-click triage app. It turns a noisy Gmail inbox
into a scannable digest grouped by what actually needs doing — reply-needed,
decisions pending, what you're waiting on, cold outreach + event invites,
newsletters/reading, in-product notifications, and pure noise — rendered as a
web tab you open like any other page. The problem it solves is inbox overload:
instead of scrolling a flat list, you see each message sorted into an
actionable bucket with a one-line, human-readable summary, and you clear each
item in a single click — archive, mark spam, unsubscribe (via the message's
`List-Unsubscribe` header), or mute (a self-managed Gmail label). There are two
parts. The `email-digest` skill does the thinking: it pulls recent Gmail,
classifies every message into the 10-bucket taxonomy using Gmail's own category
labels plus content-aware rules (and an optional LLM self-review pass), and
writes the result to a digest data file. The `email-review` web service reads
that file and renders the triage UI, performing the one-click Gmail actions
directly against the Gmail API. When it's running, the user opens a tab, scans
their inbox by category, expands any message to read it (rendered as real HTML,
with tracking pixels stripped), and clears the buckets — with an undo toast on
every action.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean forever-claude-template base):

- `libs/email_review` — the FastAPI web service (`email-review`). Its runner
  (`src/email_review/runner.py`) serves the triage UI (HTML/CSS/JS inline, no
  build step) and exposes the action endpoints; `gmail_actions.py` performs the
  archive / spam / mute / unsubscribe calls against Gmail via `latchkey curl`;
  `phishing.py` holds the shared phishing detector; `account.py` is the
  adopter-editable identity config (your name, addresses, org domains, AP
  forwarder). It reads the digest data from `runtime/email_review/data.json`.
- `.agents/skills/email-digest` — the skill that builds the digest. `SKILL.md`
  is the pipeline, `RULES.md` is the canonical (user-editable) taxonomy and
  rules, `contacts.txt` is the who's-who allowlist, and `scripts/` holds the
  classifier (`classify.py`), the LLM self-review judge (`llm_judge.py`), the
  synthesis/override writer, the mute-propagation and unsubscribe-preflight
  helpers, and the bulk-archive/contact-audit utilities.

How the pieces wire together at runtime: the skill's `classify.py` pulls recent
Gmail through the latchkey Gmail API and classifies each message, then
`synthesize_overrides.py`/`llm_judge.py` refine it, writing
`runtime/email_review/data.json`. The `email-review` FastAPI service reads that
file and renders the digest. It runs as the supervisord program
`[program:email-review]` (see `supervisord.conf`), which first registers the
port with `python3 scripts/forward_port.py --url http://localhost:8081 --name
email-review` and then launches `ROOT_PATH=/service/email-review uv run
email-review` (the app listens on **port 8081**; its entry point is
`email-review = "email_review.runner:main"` in `libs/email_review/pyproject.toml`,
and `libs/email_review` is a workspace member + dependency in the root
`pyproject.toml`). Once registered, the app is proxied at
`/service/email-review/`, so the user opens it as a workspace tab. The UI's
Refresh / Categorize buttons re-run the skill's scripts as subprocesses, and
the one-click action buttons call the service's `/api/*` endpoints, which
perform the Gmail writes.

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

- requires_permission: google-gmail-api / google-gmail-read-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- read the inbox to build the digest)
- requires_permission: google-gmail-api / google-gmail-write-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- archive, apply labels/mute, mark spam, and send one-click
  unsubscribe requests)

The classifier's LLM steps (`llm_judge.py`, `contact_audit.py`) call the
template's built-in keyless `claude -p` helper, so there is **no**
`requires_secret` — no API key needs to be provisioned.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to hook it up to their own Gmail now. If YES:
   ACTIVATE FIRST -- initiate every `requires_permission` line NOW via a
   latchkey permission request (see the `latchkey` skill; the request opens
   the approval/login flow in the minds app). Then, still as part of activation
   and BEFORE the user's first look, do the two setup steps the digest needs to
   show *correctly-sorted* data (these are Holes 1 and 2, pulled forward on
   purpose):
     a. **Identity (Hole 1).** Ask the user their name, the addresses they send
        from, and their org domain(s), and write those into `account.py`.
     b. **Contacts (Hole 2).** Run `contact_audit.py` against their now-
        connected Gmail, show them the proposed warm-contact list, and write it.
        Skip this and the first digest classifies everyone as cold.
   Only then start the services and get the app showing THE USER'S OWN DATA.
   Done for a data-backed app means the user can open it and see their own,
   correctly-sorted data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through the REMAINING holes interactively, one at a time (Holes 1 and 2
   were already handled during activation above). Translate each into plain
   language, ask for a decision only when you genuinely need one, and resolve
   the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

- **Account identity is a placeholder.** `libs/email_review/src/email_review/
  account.py` ships with fake defaults (name "Alex Doe", `alex@yourcompany.example`,
  org domain `yourcompany.example`, AP forwarder `ap@yourcompany.example`).
  Replace these with the user's real display name, every address they send
  from, their org domain(s), and their accounts-payable/forwarder address (or
  leave AP empty if they don't use one). The classifier uses these to recognize
  the user's own mail, spot impersonation, and route AP-forwarded finance mail.
- **The who's-who allowlist ships empty — the agent populates it, the user
  never hand-edits it.** `.agents/skills/email-digest/contacts.txt` contains
  only obviously-fake example rows, so until it's filled every sender looks
  "cold" and none are protected from auto-unsubscribe. Do NOT ask the user to
  type contacts into a file. Instead, once their Gmail is connected, run
  `uv run python .agents/skills/email-digest/scripts/contact_audit.py`: it mines
  the user's own sent mail, keeps everyone they actually correspond with (3+
  exchanges auto-kept; 1–2 sent to the LLM judge), and proposes them as
  `trusted-warm`. Show the user the proposed list in plain language ("I scanned
  your sent mail and found these ~N people you write to — look right?"), then
  re-run with `--write` to append them. This belongs in the ACTIVATION phase
  (see "How to adapt it") — do it before the user's first look, or their first
  digest classifies everyone as cold. The other categories (vendor, contractor,
  keep-subscribed, journalist, broker, org-fyi) take judgment: infer the obvious
  ones from the same scan and confirm the rest with a couple of quick questions.
  You fill the file on the user's behalf; they never edit the TSV.
- **The bucket taxonomy and classification rules are opinionated — the agent
  tunes them, not the user.** `RULES.md` and `classify.py` encode one person's
  10-bucket taxonomy and heuristics (which senders are automation, what counts
  as an event invite, the finance/invoice patterns, etc.). Don't hand the user
  a rules file to edit. Up front, translate the taxonomy into one plain-language
  question — "here are the ten groups I'll sort your mail into; want to rename
  or merge any?" — and make those edits for them. Then keep improving it on
  their behalf from how they actually triage: every time the user moves a
  message to a different bucket in the web view, the app appends a labeled
  record (sender, subject, content, the bucket the classifier chose and its
  reasoning, and where the user moved it) to
  `runtime/email_review/move_log.jsonl`. The engine that mines those
  corrections ships with this snapshot: `scripts/review_email_moves.py` reads
  the move log and reports any repeated correction (>= 2 same-direction moves
  that disagree with the classifier) as a candidate rule — it only *proposes*,
  it never edits rules itself. To make this recurring rather than a one-time
  pass, **during setup create a daily cron** (via `CronCreate` / the scheduling
  skill) whose prompt is roughly: "Run `cd <repo> && uv run python
  scripts/review_email_moves.py`. If it reports one or more candidate patterns,
  translate each into a concrete rule proposal (add sender X to the reading
  list, add domain Y as `trusted-warm` in `contacts.txt`, add a `classify.py`
  rule routing Z to bucket N), then message the user via the send-user-message
  skill. Make that message helpful and framed as the tool learning from them,
  not a dry list: open by noting the digest has been learning from how they
  sort their mail, then give one short plain-English line per pattern that says
  what it observed (who the mail is from and that they've moved it the same way
  N times) and asks a yes/no about the change — no bucket numbers, file names,
  or jargon. Number the proposals so they can reply with just the numbers (or
  'all'/'none'), and end by reassuring them nothing changes until they confirm.
  For example:

      Your inbox digest has been learning from how you sort your mail. A
      couple of patterns stood out this week:
      1. You keep moving newsletters from The Daily Brief into Reading — want
         me to always file them there?
      2. You've moved several emails from anyone at acme-legal.com into
         reply-needed — should I treat that domain as a real contact so they
         always surface?
      Reply with the numbers to apply (or 'all'/'none'). Nothing changes to
      how your mail is sorted until you confirm.

  Apply only what they approve; never edit `classify.py`, `contacts.txt`, or
  any rule without explicit confirmation. If the script reports no patterns,
  stay quiet and don't message the user." That daily correction loop — not
  one-time hand-editing — is how the classifier gets good for this user, and
  the user both sees it happening and stays in control of every change.
- **Reply-compose was removed.** The app is read-and-triage only; there is no
  way to draft or send a reply from the UI (the original leaned on a
  third-party mail client for this, which was stripped for the published
  version). A working replacement would add a Gmail-drafts compose box using
  the Gmail API `drafts.create`/`send` endpoints.
- **The "Muted" label name is a default.** Mute applies a Gmail label literally
  named `Muted` (created on first use), set in `gmail_actions.MUTED_LABEL_NAME`.
  If the user already uses that label name for something else, rename it there.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
