"""Tests for the shared phishing detector used by classify.py and smart-action.

Phishing detection used to live in two places that drifted out of sync; now
both callers delegate to email_review.phishing.has_phishing_tells. These
tests pin all detection signals plus the reported gaps:

  - body-name-drop ("asked by <you> to forward this invoice")
  - invoice-in-the-middle-of-subject ("#INV-2026-051") with leading prose

The account owner's name/addresses come from email_review.account (placeholder
"Alex Doe" / alex@yourcompany.example in the shipped template).
"""

from __future__ import annotations

from email_review.account import ACCOUNT_NAME
from email_review.phishing import has_phishing_tells

# The placeholder identity configured in account.py.
_NAME = ACCOUNT_NAME               # "Alex Doe"
_OWN = "alex@yourcompany.example"  # one of ACCOUNT_ADDRS


# Convenience: most callers pass (subject, frm, snippet, addr).
def check(subject="", frm="", snippet="", addr=None):
    return has_phishing_tells(subject, frm, snippet, addr)


class TestDisplayNameImpersonation:
    def test_owner_display_with_non_owner_addr_is_phishing(self):
        assert check(frm=f"{_NAME} <info@alessandrobuccioni.example>",
                     subject="INV #1100000888889q5l1")

    def test_owner_display_with_own_addr_is_not_phishing(self):
        assert not check(frm=f"{_NAME} <{_OWN}>",
                         subject="Re: SPV question")


class TestSubjectPatterns:
    def test_inv_at_start_with_random_suffix(self):
        assert check(subject="INV #1100000888889q5l1", frm="x@y.com")

    def test_inv_embedded_in_long_subject(self):
        # Invoice token at the END of a long subject.
        assert check(
            subject="Enterprise Guidance Contract Provided by Summitlink Enterprises – #INV-2026-051",
            frm="Sales <info@levelfinance.example>",
        )

    def test_po_with_dashes(self):
        assert check(subject="PO-12345-ABC needs review", frm="x@y.com")

    def test_purchase_order_full_word(self):
        assert check(subject="Purchase Order #998877", frm="x@y.com")

    def test_signature_request_phishing(self):
        assert check(subject="Signature requested for urgent document",
                     frm="x@y.com")

    def test_gift_card_scam(self):
        assert check(subject="Quick favor — need Amazon gift card today",
                     frm="x@y.com")

    def test_urgent_wire_phishing(self):
        assert check(subject="Urgent wire transfer needed", frm="x@y.com")

    def test_legit_short_invoice_number_matches(self):
        # 3+ digit invoice numbers match the token pattern by design; the
        # classifier exempts known senders elsewhere (trusted-warm, vendors).
        assert check(subject="Invoice 1234 from Vendor", frm="x@y.com")

    def test_legit_email_without_invoice_token_not_phishing(self):
        assert not check(subject="Catching up", frm="friend@example.com")


class TestBodyNameDrop:
    def test_asked_by_owner_is_phishing(self):
        assert check(
            frm="Sales <info@levelfinance.example>",
            subject="Some unrelated subject",
            snippet=f"Hello, I have been asked by {_NAME} to forward the attached invoice to you. Could you confirm receipt?",
        )

    def test_owner_requested_me_to_send(self):
        assert check(
            frm="Bad Actor <evil@scam.example>",
            subject="Important",
            snippet=f"{_NAME} requested me to send you the wire details",
        )

    def test_on_behalf_of_owner(self):
        assert check(
            frm="Bad Actor <evil@scam.example>",
            subject="Important",
            snippet=f"I am writing on behalf of {_NAME} regarding...",
        )

    def test_wire_transfer_language(self):
        assert check(
            frm="x@y.com",
            subject="Re: Banking update",
            snippet="please initiate a wire transfer to the new account",
        )

    def test_legit_body_mentioning_owner_from_owner_not_phishing(self):
        # You in your own forwarded thread referencing yourself isn't phishing.
        assert not check(
            frm=f"{_NAME} <{_OWN}>",
            subject="Re: something",
            snippet=f"I have been asked by {_NAME} (myself) to forward this",
        )


class TestTLDIsNotASignal:
    """TLD-based phishing detection was removed — too many false positives on
    legitimate senders (.xyz VCs, .info nonprofits, etc.). A bare cold-pitch
    from a spam-farm TLD must now be caught by sender content, not the TLD."""

    def test_xyz_alone_is_not_phishing(self):
        assert not check(frm="Random <stranger@thing.xyz>", subject="hello",
                         addr="stranger@thing.xyz")

    def test_shop_alone_is_not_phishing(self):
        assert not check(frm="<sender@example.shop>", subject="offer",
                         addr="sender@example.shop")

    def test_org_domain_not_suspicious(self):
        assert not check(frm="staff@yourcompany.example", subject="hi",
                         addr="staff@yourcompany.example")


class TestRegressionSpecifics:
    """Exact regression cases from past bug reports."""

    def test_impersonation_invoice_pattern(self):
        # Display-name impersonation + invoice-token subject.
        assert check(
            frm=f"{_NAME} <info@alessandrobuccioni.example>",
            subject="INV #1100000888889q5l1",
        )

    def test_body_name_drop_pattern(self):
        assert check(
            frm="Sales <info@levelfinance.example>",
            subject="Enterprise Guidance Contract Provided by Summitlink Enterprises – #INV-2026-051",
            snippet=f"Hello, I have been asked by {_NAME} to forward the attached invoice to you.",
        )
