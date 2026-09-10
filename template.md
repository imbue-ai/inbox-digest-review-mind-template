---
title: "Inbox Digest & Review"
description: "A Gmail inbox digest and one-click triage app: a skill classifies recent mail into a 10-bucket taxonomy, and a web view lets you read and clear each item (archive / spam / unsubscribe / mute)."
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

A Gmail inbox digest and one-click triage app: a skill classifies recent mail into a 10-bucket taxonomy, and a web view lets you read and clear each item (archive / spam / unsubscribe / mute).

An inbox with hundreds of unread messages hides the few that actually need a
reply behind a wall of newsletters, notifications, and cold outreach. This
template solves that by pulling everything currently in the inbox, sorting
each message into one of ten buckets (Reply needed, Decision needed, TODO,
Sent/awaiting reply, Cold outreach + events, FYI/read, Work FYI, Reading,
Marketing/spam/phishing, In-product notifications), and rendering the result
as a single triage page. From that page the user reads each thread and clears
it with one click -- archive, mark spam, mute, or unsubscribe -- straight
against their real Gmail account, with every action undoable. Re-running the
classifier (the page's Refresh / Categorize buttons) rebuilds the digest from
the current inbox state.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `system/apps/email_review`
- `.agents/skills/email-digest`
- `system/scripts/review_email_moves.py`
- `pyproject.toml`
- `system/supervisord.conf`
- `uv.lock`

`system/apps/email_review` is the tab-openable app itself: a FastAPI service
(`src/email_review/runner.py`) that renders the ten-bucket triage page and
serves the one-click Gmail actions (`gmail_actions.py`), a phishing-tell
detector (`phishing.py`) the smart-action ruleset consults, and the adopter's
own identity constants (`account.py`, ships with obvious placeholder values).
`.agents/skills/email-digest` is the agent-facing skill that actually builds a
digest: `SKILL.md` is the pipeline script, `RULES.md` is the user-editable
10-bucket taxonomy and cold-outreach methodology, `contacts.txt` is the
who's-who allowlist, and `scripts/` holds the classifier (`classify.py`), the
LLM-judge helper it and the bulk tools use (`llm_judge.py`), the periodic
contact-list refresher (`contact_audit.py`), a bulk-archive-with-exceptions
pair (`bulk_archive.py` / `bulk_archive_undo.py`), the mute-propagation and
unsubscribe-safety helpers (`propagate_mutes.py`, `check_unsub_target.py`),
and `synthesize_overrides.py` for turning one-off corrections into durable
rules. `system/scripts/review_email_moves.py` is a small standalone CLI to
review the move-log the app writes on every one-click action, independent of
the web view.

At runtime, `system/supervisord.conf` runs the app as the `email-review`
program: it registers itself on `http://localhost:8091` via
`system/scripts/forward_port.py --name email-review --icon-file
system/apps/email_review/icon.svg --program email-review`, then execs the
`email-review` console script (from `system/apps/email_review/pyproject.toml`'s
`[project.scripts]`). The program ships with `autostart=false` -- see
"Requirements" below for why -- so it will not appear as a tab until an
adopter activates it. The digest data itself (`data.json`, the move log,
starred/saved threads, bucket overrides) lives under
`data/.apps/email-review/`, written by `classify.py` and read by the app; none
of it ships with the template, so a fresh adopter sees all ten buckets
rendered empty rather than an error.

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

- requires_permission: google-gmail-api / google-gmail-read-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- builds the digest from the adopter's own inbox)
- requires_permission: google-gmail-api / google-gmail-write-all (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- powers every one-click action: archive, label/mute, mark spam,
  and the Gmail label the app creates on first use)
- requires_llm: calls Claude via the keyless subscription path (`claude -p`,
  in `llm_judge.py`, used by the content-aware classification pass and the
  contact-audit/bulk-archive helpers); an adopter on the keyed litellm path
  (`ANTHROPIC_API_KEY` set) should switch these calls per the
  `use-ai-integration` skill

- **Placeholder identity.** `account.py` ships with an obvious placeholder
  name, addresses, and org domain -- before the first real run, set these to
  the adopter's own so the classifier recognizes their outgoing mail and the
  phishing guard can spot someone impersonating them.
- **Empty contacts allowlist.** `contacts.txt` ships with example rows only
  (vendor/contractor/trusted-warm/broker/keep-subscribed categories) --
  populate it with the adopter's own who's-who, ideally via a
  `contact_audit.py --write` run against their sent mail, or the first digest
  will treat everyone as cold outreach.
- **Opinionated 10-bucket taxonomy.** `RULES.md` encodes one specific way to
  triage an inbox (the bucket list, the label pre-filter, the cold-outreach
  heuristics). An adopter may want to rename buckets, add one, or tune the
  false-positive checks to match how they actually work -- RULES.md is the
  single place to change all of it.
- **Reply-compose intentionally absent.** This app is read-and-triage only;
  it never drafts or sends mail on the adopter's behalf. An adopter who wants
  drafting could add plain Gmail drafts as a follow-up, but it is not stubbed
  in anywhere -- it would be new work.
- **Default "Muted" Gmail label name.** The mute action creates and reuses a
  label literally named `Muted` on first use; an adopter who already has a
  label by that name for something else should rename it in
  `gmail_actions.py` before first use.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting mind
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that mind's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment. The app's own
Python dependencies (FastAPI, uvicorn, Jinja2, nh3) are ordinary
`pyproject.toml` dependencies that `uv sync --all-packages` already installs;
the only external tool any included script shells out to is `latchkey`
itself, which every mind already has.

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

### v1 (2026-09-09) -- initial publish, re-cut from kanjun's original inbox-digest-review onto a current mind base (minds-v0.5.0), with the app under `system/apps/` and the empty-first-load bug already fixed upstream of this snapshot.

## Adaptation history

Each mind that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
