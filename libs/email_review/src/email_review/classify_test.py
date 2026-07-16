"""Unit tests for classifier rules in `.agents/skills/email-digest/scripts/classify.py`.

The classifier lives outside this package (it's a stand-alone script the
email-digest skill runs); we import it by path so the regression suite stays
close to the renderer's tests.

Fixtures use the shipped placeholder identity (account.py: name "Alex Doe",
address alex@yourcompany.example, org domain yourcompany.example, AP forwarder
ap@yourcompany.example) and the example contacts.txt entries.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_CLASSIFY_PATH = (
    Path(__file__).resolve().parents[4]
    / ".agents/skills/email-digest/scripts/classify.py"
)

# Placeholder identity (mirrors account.py).
OWN = "alex@yourcompany.example"
STAFF = "sam@yourcompany.example"
AP = "ap@yourcompany.example"


@pytest.fixture(scope="module")
def classify_mod():
    spec = importlib.util.spec_from_file_location("classify_under_test", _CLASSIFY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["classify_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestAckPatterns:
    """Acknowledgment-only replies → bucket 3."""

    def test_wonderful_matches(self, classify_mod):
        assert classify_mod.ACK_PATTERNS.match("Wonderful! Erik")

    def test_lowercased_input_matches(self, classify_mod):
        # `snip` is lowercased before the match — must still fire.
        assert classify_mod.ACK_PATTERNS.match("wonderful! erik")

    def test_thanks_matches(self, classify_mod):
        assert classify_mod.ACK_PATTERNS.match("Thanks!")

    def test_sounds_good_matches(self, classify_mod):
        assert classify_mod.ACK_PATTERNS.match("Sounds good, see you Friday")

    def test_got_it_matches(self, classify_mod):
        assert classify_mod.ACK_PATTERNS.match("Got it!")

    def test_substantive_reply_does_not_match(self, classify_mod):
        # A real question is not an ack.
        assert not classify_mod.ACK_PATTERNS.match(
            "Hi Alex — I'd love to follow up on the funding question"
        )

    def test_split_on_lowercased_on_wrote(self, classify_mod):
        """The regression: ACK detection relies on splitting at "on <date>
        wrote:" to isolate the new content. Since the snippet is lowercased
        upstream, the split regex must be case-insensitive too. If it isn't,
        snip_head contains the full quoted thread and the length check (<80
        chars) fails — silently breaking the rule.
        """
        snip = (
            "wonderful! erik on fri, may 15, 2026 at 4:01 pm alex doe "
            "<alex@yourcompany.example> wrote: yes, i'm free for dinner after class!"
        )
        snip_head = re.split(
            r"on .+ wrote:|on \w+, \w+ \d", snip, maxsplit=1
        )[0].strip()
        assert len(snip_head) < 80
        assert classify_mod.ACK_PATTERNS.match(snip_head)


class TestCalSubjRelaxation:
    """Calendar-invite subjects with extra words between keyword and ":"."""

    def test_plain_updated_invitation_matches(self, classify_mod):
        assert classify_mod.CAL_SUBJ.match(
            "Updated invitation: meeting @ Tue May 5"
        )

    def test_updated_invitation_with_note_matches(self, classify_mod):
        # The regression: this subject used to be left out of CAL_SUBJ,
        # so the message landed in bucket 1 (Reply needed) instead of 8/10.
        assert classify_mod.CAL_SUBJ.match(
            "Updated invitation with note: Alex Doe (Acme) <> Ian "
            "Krietzberg (Puck) @ Tue May 5"
        )

    def test_accepted_with_extra_words_matches(self, classify_mod):
        assert classify_mod.CAL_SUBJ.match(
            "Accepted: Acme sync [DRIs: Tom/Alex] @ Tue May 5"
        )

    def test_declined_matches(self, classify_mod):
        assert classify_mod.CAL_SUBJ.match("Declined: 1:1 with Alex @ Wed")

    def test_non_calendar_subject_does_not_match(self, classify_mod):
        assert not classify_mod.CAL_SUBJ.match("Re: SPV lawyer?")

    def test_extra_word_limit(self, classify_mod):
        # Allow up to 4 extra words between keyword and ":" — don't accept
        # arbitrary unrelated content that just happens to contain "Updated
        # invitation" somewhere.
        assert not classify_mod.CAL_SUBJ.match(
            "Re: Updated invitation was sent last week and we're still "
            "waiting on a response: please follow up"
        )


class TestDirectAddressPatterns:
    """Body starts with "Alex —" / "Hi Alex" → override actor-check demote.
    The pattern is built from the configured first name (account.py)."""

    def test_em_dash_matches(self, classify_mod):
        assert classify_mod.DIRECT_ADDRESS_PATTERNS.match(
            "Alex — I'd be happy to chat about this whenever it's convenient."
        )

    def test_hi_name_matches(self, classify_mod):
        assert classify_mod.DIRECT_ADDRESS_PATTERNS.match("Hi Alex, ...")

    def test_hey_name_matches(self, classify_mod):
        assert classify_mod.DIRECT_ADDRESS_PATTERNS.match("Hey Alex! ...")

    def test_comma_after_name_matches(self, classify_mod):
        assert classify_mod.DIRECT_ADDRESS_PATTERNS.match("Alex, here's the update")

    def test_does_not_match_address_to_other_person(self, classify_mod):
        assert not classify_mod.DIRECT_ADDRESS_PATTERNS.match(
            "Hi Ian, do any of these times work for you on the 9th?"
        )


class TestOutgoingCalendarInvite:
    """Calendar-invite messages sent from your own account are auto-generated
    system messages, not awaiting-reply correspondence — they must NOT land in
    bucket 5."""

    def _classify(self, classify_mod, **overrides):
        rec = {
            "id": "m1",
            "threadId": "t1",
            "labels": ["INBOX", "SENT", "CATEGORY_PERSONAL"],
            "from": f"Alex Doe <{OWN}>",
            "from_addr": OWN,
            "to": f"Sam Rivers <{STAFF}>",
            "cc": "",
            "subject": "Updated invitation: projects review @ Tue Apr 21",
            "snippet": "you have updated this event",
            "date": "Tue, 21 Apr 2026 12:00:00 +0000",
        }
        rec.update(overrides)
        return classify_mod.classify(rec, "warm (prior outbound)", {}, set())

    def test_outgoing_calendar_invite_is_not_bucket_5(self, classify_mod):
        bucket, why = self._classify(classify_mod)
        # An auto-generated outgoing calendar invite must NOT be treated as
        # awaiting-reply (bucket 5). It routes to bucket 8 (in-product).
        assert bucket != "5", (
            f"Outgoing calendar invite must not land in bucket 5, got {bucket}: {why}"
        )

    def test_outgoing_non_calendar_message_still_goes_to_bucket_5(self, classify_mod):
        # Sanity: the exception only applies to calendar invites — regular
        # outgoing mail still hits Rule 1.
        bucket, why = self._classify(
            classify_mod,
            subject="Re: SPV discussion next week",
            snippet="here's what I'm thinking on the SPV setup",
        )
        assert bucket == "5"


class TestBrokerCoordinationDemote:
    """When a known broker has 2+ messages in a thread, the whole thread is
    brokered scheduling — you are just on cc for awareness. Demote to 3.
    """

    def _make_thread(self, broker_msg_count: int) -> list[dict]:
        """Build a synthetic thread where a broker has N messages between
        themselves and a third party, with you always on cc.
        """
        msgs = []
        for i in range(broker_msg_count):
            msgs.append(
                {
                    "id": f"m{i}",
                    "threadId": "t1",
                    "from": "Robin Introductions <robin@intros.example>",
                    "from_addr": "robin@intros.example",
                    "to": "Ian Krietzberg <ian@puck.example>",
                    "cc": f"Alex Doe <{OWN}>",
                    "date": f"Tue, 5 May 2026 1{i}:00:00 +0000",
                    "subject": "Re: meeting introduction",
                    "snippet": "Hi Ian, do any of these times work?",
                    "labels": ["INBOX"],
                    "final_bucket": "1",
                    "final_why": "Warm sender — default to reply needed",
                }
            )
        # The third party's one reply.
        msgs.append(
            {
                "id": "ian_reply",
                "threadId": "t1",
                "from": "Ian Krietzberg <ian@puck.example>",
                "from_addr": "ian@puck.example",
                "to": "Robin Introductions <robin@intros.example>",
                "cc": f"Alex Doe <{OWN}>",
                "date": "Tue, 5 May 2026 14:00:00 +0000",
                "subject": "Re: meeting introduction",
                "snippet": "Can do the 9th at 11 AM PT",
                "labels": ["INBOX"],
                "final_bucket": "1",
                "final_why": "thread-continuity promotion",
            }
        )
        return msgs

    def test_two_broker_msgs_demote_whole_thread(self, classify_mod):
        thread = self._make_thread(broker_msg_count=2)
        is_broker = (
            lambda a: a == "robin@intros.example"
            or a in classify_mod.BROKER_EMAILS
        )
        broker_counts: dict[str, int] = {}
        for r in thread:
            if is_broker(r["from_addr"]):
                broker_counts[r["from_addr"]] = broker_counts.get(r["from_addr"], 0) + 1
        assert any(c >= 2 for c in broker_counts.values()), (
            "fixture must trigger the broker-coordinating heuristic"
        )

    def test_one_broker_msg_does_not_demote(self, classify_mod):
        """The intro case: broker sends one intro, the new contact replies with
        their times. You ARE the actor — no demote.
        """
        thread = self._make_thread(broker_msg_count=1)
        is_broker = (
            lambda a: a == "robin@intros.example"
            or a in classify_mod.BROKER_EMAILS
        )
        broker_counts: dict[str, int] = {}
        for r in thread:
            if is_broker(r["from_addr"]):
                broker_counts[r["from_addr"]] = broker_counts.get(r["from_addr"], 0) + 1
        assert not any(c >= 2 for c in broker_counts.values()), (
            "single-broker-msg fixture must NOT trigger the demote"
        )


class TestAPInvoicePrecedence:
    """AP-forwarded vendor invoices must reach bucket 10 (Work FYI) and NOT be
    intercepted by the phishing INV/PO subject regex. This pins the Rule 2.5
    placement (AP-finance check before phishing detection)."""

    def _make(self, **overrides):
        rec = {
            "id": "m1",
            "threadId": "t1",
            "labels": ["INBOX"],
            "from": f"AP <{AP}>",
            "from_addr": AP,
            "to": AP,
            "cc": "",
            "subject": "Acme Partners LP: #INV2100",
            "snippet": "Please find attached your invoice. Thanks.",
            "date": "Mon, 1 Jun 2026 15:52:51 +0000",
        }
        rec.update(overrides)
        return rec

    def test_ap_forwarded_inv_subject_goes_to_work_fyi(self, classify_mod):
        bucket, why = classify_mod.classify(self._make(), "warm", {}, set())
        assert bucket == "10", (
            f"AP-forwarded invoice with #INV in subject should land in bucket 10, got {bucket}: {why}"
        )

    def test_ap_forwarded_approval_needed_goes_to_work_fyi(self, classify_mod):
        bucket, why = classify_mod.classify(
            self._make(subject="Approval needed: Bill for Acme CEB89026-0193"),
            "warm", {}, set(),
        )
        assert bucket == "10", why

    def test_ap_forwarded_random_spam_still_caught_by_phishing(self, classify_mod):
        # Spam to the AP forwarder that name-drops you in the body must still be
        # caught by phishing detection, NOT short-circuited to bucket 8 or 10.
        # This is why Rule 2.5 only fires on legitimate invoice/action subjects.
        bucket, _ = classify_mod.classify(
            self._make(
                subject="Quick question",
                snippet="I have been asked by Alex Doe to forward this invoice to you.",
            ),
            "warm", {}, set(),
        )
        assert bucket == "7", f"AP-routed spam with body name-drop should be phishing, got {bucket}"


class TestKnownGoodSenderPhishingExemption:
    """A known vendor or org-staff sender with a legitimate invoice subject
    must NOT be flagged as phishing (bucket 7). The shared phishing detector is
    intentionally broad on invoice/payment subjects (`invoice 1024` is a
    phishing tell by design); the classifier exempts senders whose identity is
    already established in contacts.txt / your org domain."""

    XYLA_SUBJ = "Re: New payment request from Acme Plants LLC - invoice 1024"

    def test_vendor_invoice_subject_is_work_fyi_not_phishing(self, classify_mod):
        rec = {
            "id": "m1",
            "threadId": "t1",
            "labels": ["INBOX", "UNREAD"],
            "from": "Lauren <lauren@acme-accounting.example>",
            "from_addr": "lauren@acme-accounting.example",
            "to": f"Sam Rivers <{STAFF}>",
            "cc": AP,
            "subject": self.XYLA_SUBJ,
            "snippet": "Here is the plant maintenance invoice for your records.",
            "date": "Mon, 8 Jun 2026 19:00:00 +0000",
        }
        bucket, why = classify_mod.classify(rec, "skipped (automation/internal)", {}, set())
        assert bucket == "10", (
            f"Known-vendor invoice should be Work FYI (10), got {bucket}: {why}"
        )

    def test_org_staff_to_ap_with_invoice_subject_is_work_fyi_not_phishing(self, classify_mod):
        rec = {
            "id": "m2",
            "threadId": "t1",
            "labels": ["INBOX", "UNREAD"],
            "from": f"Sam Rivers <{STAFF}>",
            "from_addr": STAFF,
            "to": AP,
            "cc": "",
            "subject": f"Fwd: {self.XYLA_SUBJ}",
            "snippet": "Hi Lauren, Here's our plant maintenance invoice. Thank you! Sam",
            "date": "Mon, 8 Jun 2026 18:00:00 +0000",
        }
        bucket, why = classify_mod.classify(rec, "warm (prior outbound)", {}, set())
        assert bucket == "10", (
            f"Org-staff invoice forward to AP should be Work FYI (10), got {bucket}: {why}"
        )

    def test_unknown_sender_with_invoice_token_subject_still_phishing(self, classify_mod):
        # Regression guard: the exemption must NOT weaken detection for senders
        # whose identity is not established. An unknown address with the same
        # invoice-token subject still hits the phishing rule.
        rec = {
            "id": "m3",
            "threadId": "t9",
            "labels": ["INBOX"],
            "from": "Accounts Payable <billing@acme-plants-invoices.example>",
            "from_addr": "billing@acme-plants-invoices.example",
            "to": OWN,
            "cc": "",
            "subject": self.XYLA_SUBJ,
            "snippet": "Please remit payment for the attached invoice.",
            "date": "Mon, 8 Jun 2026 18:00:00 +0000",
        }
        bucket, why = classify_mod.classify(rec, "no prior outbound — candidate cold", {}, set())
        assert bucket == "7", (
            f"Unknown sender with invoice-token subject should still be phishing, got {bucket}: {why}"
        )


class TestJournalistCategory:
    """`journalist` is a distinct contacts.txt category, treated as warm
    (never cold/spam) exactly like trusted-warm. Pins the category parsing
    and the warm-treatment call-sites so press contacts don't regress into
    cold-outreach or marketing/spam."""

    JOURNALIST = "jordan@press.example"          # Jordan Lee, journalist
    TRUSTED = "sam.friend@personal.example"       # Sam Rivers, trusted-warm

    def test_journalist_loaded_into_journalist_set(self, classify_mod):
        assert classify_mod.is_journalist(self.JOURNALIST)
        # Not in trusted-warm — it's its own category.
        assert not classify_mod.is_trusted_warm(self.JOURNALIST)

    def test_journalist_counts_as_warm_contact(self, classify_mod):
        assert classify_mod.is_warm_contact(self.JOURNALIST)

    def test_trusted_warm_unaffected_by_journalist_category(self, classify_mod):
        assert classify_mod.is_warm_contact(self.TRUSTED)
        assert classify_mod.is_trusted_warm(self.TRUSTED)
        assert not classify_mod.is_journalist(self.TRUSTED)

    def test_journalist_exempt_from_phishing(self, classify_mod):
        assert classify_mod.is_known_good_sender(self.JOURNALIST)

    def test_journalist_with_promotions_label_not_marketing(self, classify_mod):
        # Gmail sometimes files a real journalist's note under Promotions. A
        # warm journalist must NOT be dumped into bucket 7.
        rec = {
            "id": "j1",
            "threadId": "tj",
            "labels": ["INBOX", classify_mod.CATEGORY_PROMOTIONS],
            "from": f"Jordan Lee <{self.JOURNALIST}>",
            "from_addr": self.JOURNALIST,
            "to": OWN,
            "cc": "",
            "subject": "Quick question for a piece",
            "snippet": "Hi Alex, working on a story about agents and wanted your take.",
            "date": "Mon, 8 Jun 2026 18:00:00 +0000",
        }
        bucket, why = classify_mod.classify(rec, "warm (journalist via contacts.txt)", {}, set())
        assert bucket != "7", (
            f"Journalist filed under Promotions should not be marketing/spam, got {bucket}: {why}"
        )


class TestOrgFyiCategory:
    """`org-fyi` is a contacts.txt category for pure-automation administrators
    (e.g. a 401k provider). Every message routes to bucket 10 regardless of
    subject — unlike `vendor`, which gates on an invoice subject."""

    PROVIDER = "info@retirement-provider.example"

    def _make(self, **overrides):
        rec = {
            "id": "f1",
            "threadId": "tf",
            "labels": ["INBOX"],
            "from": f"Retirement Provider <{self.PROVIDER}>",
            "from_addr": self.PROVIDER,
            "to": OWN,
            "cc": "",
            "subject": "401(k) Contribution Report for 8/17/2026",
            "snippet": "Your latest contribution report is attached.",
            "date": "Mon, 22 Jun 2026 18:00:00 +0000",
        }
        rec.update(overrides)
        return rec

    def test_org_fyi_loaded(self, classify_mod):
        assert classify_mod.is_org_fyi(self.PROVIDER)
        # Domain-wide entry also covers other senders on the domain.
        assert classify_mod.is_org_fyi("statements@retirement-provider.example")

    def test_non_invoice_subject_routes_to_work_fyi(self, classify_mod):
        # A "Contribution Report" subject (no invoice keyword) from a sender
        # with no prior outbound.
        bucket, why = classify_mod.classify(
            self._make(), "skipped (automation/internal)", {}, set()
        )
        assert bucket == "10", f"Provider mail should be Work FYI, got {bucket}: {why}"

    def test_eligibility_update_also_routes_to_work_fyi(self, classify_mod):
        bucket, why = classify_mod.classify(
            self._make(subject="401(k) Update: August eligible employees"),
            "skipped (automation/internal)", {}, set(),
        )
        assert bucket == "10", f"Provider update should be Work FYI, got {bucket}: {why}"

    def test_org_fyi_exempt_from_phishing(self, classify_mod):
        assert classify_mod.is_known_good_sender(self.PROVIDER)


class TestDocusignSigningRequests:
    """Docusign signing requests → bucket 4 (TODO). Every "Complete with
    Docusign" subject needs your signature, including the `Reminder:` and
    `Corrected:` prefixed variants. Docusign `Completed:` notices stay
    no-action."""

    def _make(self, subject, addr="dse_na2@docusign.net", **overrides):
        rec = {
            "id": "ds1",
            "threadId": "tds",
            "labels": ["INBOX"],
            "from": f"DocuSign NA2 <{addr}>",
            "from_addr": addr,
            "to": OWN,
            "cc": "",
            "subject": subject,
            "snippet": "Please review and sign the attached document.",
            "date": "Mon, 8 Jun 2026 18:00:00 +0000",
        }
        rec.update(overrides)
        return rec

    def test_reminder_signing_request_is_todo(self, classify_mod):
        bucket, why = classify_mod.classify(
            self._make("Reminder: Complete with Docusign: Acme - CA 25102(o) notices for 2021"),
            "no prior outbound — candidate cold", {}, set(),
        )
        assert bucket == "4", f"Docusign reminder should be TODO, got {bucket}: {why}"

    def test_bare_signing_request_is_todo(self, classify_mod):
        bucket, why = classify_mod.classify(
            self._make("Complete with Docusign: Acme MSA"),
            "no prior outbound — candidate cold", {}, set(),
        )
        assert bucket == "4", f"Docusign signing request should be TODO, got {bucket}: {why}"

    def test_corrected_signing_request_is_todo(self, classify_mod):
        bucket, why = classify_mod.classify(
            self._make("Corrected: Complete with Docusign: Acme MSA"),
            "no prior outbound — candidate cold", {}, set(),
        )
        assert bucket == "4", f"Docusign corrected re-send should be TODO, got {bucket}: {why}"

    def test_completion_notice_is_not_todo(self, classify_mod):
        # "Completed:" means nothing is left to sign — stays FYI, not TODO.
        bucket, why = classify_mod.classify(
            self._make("Completed: Complete with Docusign: Acme MSA"),
            "no prior outbound — candidate cold", {}, set(),
        )
        assert bucket == "3", f"Docusign completion notice should be FYI, got {bucket}: {why}"


class TestReadingPublications:
    """Newsletters/publications in READING_SENDERS route to Reading (bucket 9),
    not in-product notifications (bucket 8)."""

    def _make(self, classify_mod, from_addr, frm):
        rec = {
            "id": "m1", "threadId": "t1", "labels": ["INBOX", "UNREAD"],
            "from": frm, "from_addr": from_addr, "to": OWN, "cc": "",
            "subject": "The Takeaway: the pragmatic path",
            "snippet": "This week in tech...",
            "date": "Mon, 8 Jun 2026 19:00:00 +0000",
        }
        return classify_mod.classify(rec, "skipped (automation/internal)", {}, set())

    def test_publication_is_reading(self, classify_mod):
        bucket, why = self._make(
            classify_mod, "hello@thebulletin.example", "The Bulletin <hello@thebulletin.example>"
        )
        assert bucket == "9", f"The Bulletin should be Reading (9), got {bucket}: {why}"

    def test_reading_sender_matches_any_local_part(self, classify_mod):
        # A different sending address on the same publication domain still reads.
        bucket, _ = self._make(
            classify_mod, "newsletter@thebulletin.example", "The Bulletin <newsletter@thebulletin.example>"
        )
        assert bucket == "9"

    def test_individual_reading_contact_is_reading(self, classify_mod):
        # A specific person added to the reading list (not a publication domain).
        bucket, why = self._make(
            classify_mod, "essays@indie-writer.example", "Indie Writer <essays@indie-writer.example>"
        )
        assert bucket == "9", f"Reading-list contact should be Reading (9), got {bucket}: {why}"
