---
title: "Inbox Digest & Review"
description: "A Gmail inbox digest and one-click triage app: a skill classifies recent mail into ten actionable buckets, and a web tab lets you read and clear each item (archive / spam / unsubscribe / mute)."
thumbnail: "template.svg"
version: v1
format: v2
---

# Inbox Digest & Review

This file is the manifest for the **Inbox Digest & Review** template (slug:
`inbox-digest-review`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a mind that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A Gmail inbox digest and one-click triage app: a skill classifies recent mail into ten actionable buckets, and a web tab lets you read and clear each item (archive / spam / unsubscribe / mute).

The problem it solves is inbox overload. Instead of scrolling a flat list of
everything that arrived, you get your recent mail sorted into ten groups that
map to what you would actually do about each one: what needs a reply, what
decision is pending, what you are waiting on someone else for, cold outreach
and event invites, newsletters and reading, in-product notifications, and pure
noise. Every message carries a one-line, human-readable summary of why it is
where it is. You clear each one in a single click -- archive, mark spam,
unsubscribe (using the message's own unsubscribe header), or mute a thread --
with an undo on every action, and you can expand any message to read it in
place, rendered as the real email with tracking pixels stripped rather than as
escaped source text.

There are two halves. A skill does the thinking: it pulls recent Gmail,
classifies every message using Gmail's own category labels plus content-aware
rules and an optional LLM review pass, and writes the result to a data file. A
web app reads that file, renders the triage view as a tab you open like any
other page, and performs the Gmail writes when you click. It also learns: every
time you move a message to a different bucket, that correction is logged as
labelled data, and a shipped script mines the log for patterns worth turning
into rules -- so the sorting gets closer to how you actually triage, with you
approving each change.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `.agents/skills/email-digest`
- `system/apps/email_review`
- `system/scripts/review_email_moves.py`
- `system/supervisord.conf`
- `uv.lock`

What each one is:

- **`.agents/skills/email-digest`** -- the skill that builds the digest, and
  the half that does the thinking. `SKILL.md` is the pipeline it follows;
  `RULES.md` is the canonical, user-editable statement of the ten-bucket
  taxonomy and the classification rules; `contacts.txt` is the who's-who
  allowlist that decides which senders count as warm. Its `scripts/` hold the
  classifier itself, the LLM review pass, the synthesis writer that produces
  each message's one-line summary, a contact-audit tool that proposes the
  allowlist from the user's own sent mail, and helpers for mute propagation,
  unsubscribe preflight, and bulk archiving.
- **`system/apps/email_review`** -- the web app, and the half you look at. Its
  runner serves the triage page and the settings page (HTML, CSS, and JS
  inline, so there is no build step) and exposes the action endpoints; a
  Gmail-actions module performs the archive, spam, mute, and unsubscribe calls;
  a phishing module holds the shared detector; and an account module is the
  adopter-editable identity config. `app.toml` and `icon.svg` are what let the
  workspace register it as a tab.
- **`system/scripts/review_email_moves.py`** -- the learning loop's engine. It
  reads the log of manual bucket moves and reports any repeated correction that
  disagrees with the classifier as a candidate rule. It only ever proposes;
  it never edits rules itself.
- **`system/supervisord.conf`** and **`uv.lock`** -- the wiring and the
  dependency pins, included so the app runs as published rather than needing an
  adopter to reconstruct its program entry.

How they wire together at runtime: the skill's classifier pulls recent Gmail
through the workspace's credential gateway and classifies each message, the
synthesis and LLM-review scripts refine that, and the result is written to
`data/.apps/email-review/data.json`. The `email-review` program in
`system/supervisord.conf` registers the app's port and manifest through
`system/scripts/forward_port.py` and then launches the app, which reads that
data file and renders the digest. Once registered, it appears as a workspace
tab. The page's Refresh and Categorize buttons re-run the skill's scripts as
subprocesses, and the one-click action buttons call the app's own endpoints,
which perform the Gmail writes. Manual moves are appended to
`data/.apps/email-review/move_log.jsonl`, which is what the move-review script
later mines.

Two deliberate choices worth knowing before you change them. The program ships
with **autostart off**: until a Gmail grant exists the classifier cannot
produce a digest, so starting it would register a tab that can only ever render
an empty page. Turn it on during activation, once the first digest exists. And
the app's root-path environment variable is left **unset** on purpose -- a
workspace at this version registers apps by origin rather than proxying them
behind a path prefix, and the page derives its own prefix client-side, so
setting it would be wrong here.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting mind must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.

### Activation

Note that BOTH lines below are needed. Read alone is not enough for a working
app: the digest can be built, but every button that clears an item from the
inbox is a Gmail write, so a read-only grant leaves the user able to see their
mail sorted and unable to act on it.

- requires_permission: google-gmail-api / google-gmail-read-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- read the inbox to build the digest)
- requires_permission: google-gmail-api / google-gmail-write-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- archive, apply the mute label, mark spam, and send one-click
  unsubscribe requests)
- requires_llm: the classifier's review and contact-audit steps call Claude
  through the workspace's built-in KEYLESS subscription helper, so no API key
  needs provisioning and there is no secret to set. An adopter whose workspace
  is set up for the keyed path instead should switch those calls per the
  use-ai-integration skill.

There is no `requires_secret` for this template.

### Adaptation

- **The account identity is a placeholder.** The app's account module ships
  with fake defaults -- a made-up name, address, org domain, and
  accounts-payable forwarder. Replace them with the user's real display name,
  every address they send from, their org domain(s), and their AP or forwarder
  address if they use one (leave it empty if not). The classifier uses these to
  recognise the user's own mail, spot impersonation, and route forwarded
  finance mail, so leaving them stubbed misclassifies from the first run.

- **The who's-who allowlist ships empty -- the agent fills it, the user never
  hand-edits it.** `contacts.txt` contains only obviously-fake example rows, so
  until it is populated every sender looks cold and none are protected from
  auto-unsubscribe. Do NOT ask the user to type contacts into a file. Once
  their Gmail is connected, run the skill's `contact_audit.py`: it mines their
  own sent mail, auto-keeps everyone they exchange with three or more times,
  and sends the one-or-two-exchange cases to the LLM judge. Show the proposed
  list in plain language ("I scanned your sent mail and found these N people
  you write to -- look right?"), then re-run with `--write` to append them.
  This belongs in ACTIVATION, before the user's first look: skip it and their
  first digest classifies everyone as cold. The other categories (vendor,
  contractor, keep-subscribed, journalist, broker, org-fyi) take judgement --
  infer the obvious ones from the same scan and confirm the rest with a couple
  of quick questions.

- **The bucket taxonomy and classification rules are opinionated -- the agent
  tunes them, not the user.** `RULES.md` and the classifier encode one person's
  ten-bucket taxonomy and heuristics: which senders count as automation, what
  counts as an event invite, the finance and invoice patterns. Do not hand the
  user a rules file to edit. Up front, translate the taxonomy into one
  plain-language question -- "here are the ten groups I will sort your mail
  into; want to rename or merge any?" -- and make those edits for them. Then
  keep improving it on their behalf from how they actually triage: every manual
  move in the web view appends a labelled record to the move log, and
  `system/scripts/review_email_moves.py` mines that log and reports any
  repeated correction as a candidate rule. To make this recurring rather than a
  one-off, create a daily job during setup that runs the script and, when it
  finds patterns, messages the user with numbered, plain-English yes/no
  proposals framed as the tool learning from them -- no bucket numbers or file
  names -- ending with a note that nothing changes until they confirm. Apply
  only what they approve; never edit the rules, the classifier, or the contact
  list without explicit confirmation. If the script finds nothing, stay quiet.

- **Reply-compose was removed.** The app is read-and-triage only; there is no
  way to draft or send a reply from the page. The original leaned on a
  third-party mail client for this, which was stripped before publishing. A
  working replacement would add a compose box backed by the Gmail drafts
  endpoints.

- **The mute label name is a default.** Muting applies a Gmail label with a
  fixed default name, created on first use. If the user already uses that label
  for something else, rename it in the app's Gmail-actions module.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting mind
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that mind's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

For the record, since "nothing" is worth being able to trust: the app's own
dependencies (fastapi, uvicorn, jinja2, nh3) are ordinary python requirements
declared in its `pyproject.toml` and resolved by the workspace's normal sync;
the skill's scripts are standard library plus the workspace's own credential
and LLM helpers; and every Gmail call goes out over HTTP rather than through a
client library. Nothing here shells out to a binary that has to be installed.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new mind. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-09-09) -- first publication on the minds-v0.5.1 base, re-cut from a predecessor published two versions back

This is the first version of this template published against the current
workspace layout. It is not a new app: the digest skill and the triage web app
were originally published by another mind as `inbox-digest-review`, built on
the minds-v0.3.3 base, where apps lived under a repo-root `libs/`, scripts and
`supervisord.conf` sat at the root, and app manifests did not exist yet.

That predecessor could not be adopted by a current workspace by merging it --
its tree carries a whole 0.3.3 base, and merging it conflicts with roughly
fifty scaffolding files and replaces them with older copies. So this version
was assembled the other way round: a clean minds-v0.5.1 base, with only the
three paths the template actually contributes re-applied at their current
locations and rewired for the current conventions. The full set of rewiring
rules is in `template.toml`'s `modification_rules`.

One change is a genuine bug fix rather than a relayout, and it is not specific
to any workspace version: the digest page and its bucket-fragment endpoint both
read the digest data file unconditionally, so both returned a server error on
any workspace whose classifier had never run -- which is every workspace at the
moment it adopts this. A missing file is now treated as the empty digest, with
tests covering both endpoints.

## Adaptation history

Each mind that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
