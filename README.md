<p align="center">
  <img alt="Inbox Digest & Review" src="template.svg" width="480">
</p>

# Inbox Digest & Review

<p align="center">
  <a href="https://boweiliu.github.io/open-in-minds/?git_url=https://github.com/imbue-ai/inbox-digest-review-mind-template"><img alt="Open in Minds" height="64" src="https://img.shields.io/badge/Open%20in%20Minds-D8D1C0?style=for-the-badge"></a>
</p>

Didn't work? Create a Minds workspace and paste this to your agent:
` /use-template https://github.com/imbue-ai/inbox-digest-review-mind-template`

## Why you care

A Gmail inbox digest and one-click triage app: a skill classifies recent mail into ten actionable buckets, and a web tab lets you read and clear each item (archive / spam / unsubscribe / mute).

A busy inbox is a flat list that tells you nothing about what to do next, so
triage means opening things one at a time to find out whether they matter. This
sorts your recent mail into groups that match the decisions you would actually
make about it -- reply, decide, wait, read, ignore -- and lets you clear each
one without leaving the page.

## How to use it

Once it is running you open one tab. The page shows your recent mail grouped
into ten buckets, each message with a one-line summary of why it landed there:

| Bucket | What lands there |
| --- | --- |
| Reply needed | Someone is waiting on words from you |
| Decision needed | Something is blocked on a call you have to make |
| TODO | An action that is not a reply (sign this, book that) |
| Sent / awaiting reply | You wrote; the ball is in their court |
| Cold outreach + event invites | Strangers pitching or inviting |
| FYI / read | Addressed to you, but nothing to do |
| Work FYI | Org and finance traffic worth seeing, not acting on |
| Reading | Newsletters and publications you chose to get |
| Marketing / spam / phishing | Noise, including impersonation attempts |
| In-product notifications | Machines telling you a thing happened |

The day-to-day loop:

1. **Refresh** pulls your recent mail and re-runs the classifier.
2. **Categorize** runs the LLM review pass over the result, which re-buckets
   anything the rules got wrong.
3. Scan the buckets. Expand any message to read it in place -- rendered as the
   real email, with tracking pixels stripped.
4. Clear it in one click: **archive**, **spam**, **unsubscribe** (uses the
   message's own unsubscribe header), or **mute** the thread. Every action has
   an undo.
5. Disagree with where something landed? Move it. That correction is logged.

That last step is the part worth knowing about. `review_email_moves.py` reads
the log of your manual moves and reports any correction you have made twice or
more in the same direction as a candidate rule. Wire it to a daily job and the
sorting converges on how you actually triage -- it proposes, you approve, and
nothing changes to your rules without your say-so.

There is also a **Settings & rules** page for editing the taxonomy, the
classification rules, and the contact allowlist directly.

## Ideas for making it yours

- **Have it come to you instead of you to it.** Add a morning job that builds
  the digest and messages you the counts, so the tab is where you go when
  something needs clearing rather than something you remember to check.
- **Point it at a different mailbox.** Nothing about the taxonomy is
  Gmail-specific; the Gmail-actions module is the only part that talks to
  Gmail, so another provider is a swap of that one layer.
- **Add a bucket the ten do not cover.** Recruiting, customer escalations, and
  anything-from-my-manager are common additions -- a bucket is an entry in the
  rules file plus a section on the page.
- **Auto-clear the buckets you never read.** In-product notifications and
  marketing are usually read-never; a rule that archives them on arrival turns
  two buckets into zero clicks.
- **Put the learning loop on a leash, or take it off one.** The move-review
  script only proposes. If you trust it, let it apply high-confidence patterns
  itself; if you want more control, raise the number of repeat corrections it
  takes before it says anything.

## What this is

This repository is a published **minds template**: a clean, bootable
snapshot of what a mind built, ready to adapt into your own. It is NOT the
generic workspace template -- it is this specific project.

[`template.md`](template.md) is the full manifest -- what it is, how it
works, what it needs to run, and what to adapt -- with the
machine-readable half (recipe, requirements, and the environment it needs
installed) in [`template.toml`](template.toml).
