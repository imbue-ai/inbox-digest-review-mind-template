"""Unit tests for the internal-forwarder guard in gmail_actions.

The bug we're pinning: an "unsubscribe" attempt on a message routed through a
shared org forwarder pointed the List-Unsubscribe header at the group's
manage-subscription page — following it would remove you from your own
forwarder. The guard refuses these links by inspecting the header value for
your own org domains (account.ORG_DOMAINS).
"""

from __future__ import annotations

from email_review.gmail_actions import _is_internal_forwarder_unsub

# The placeholder org domain configured in account.py.
_ORG = "yourcompany.example"


class TestInternalForwarderGuard:
    """Refuse List-Unsubscribe links that point back at your own domains."""

    def test_org_groups_url_is_blocked(self):
        # The classic internal-forwarder incident: a group's subscribe page.
        header = f"<https://groups.google.com/a/{_ORG}/group/ap/subscribe>"
        assert _is_internal_forwarder_unsub(header)

    def test_org_mailto_is_blocked(self):
        header = f"<mailto:ap+unsubscribe@{_ORG}>"
        assert _is_internal_forwarder_unsub(header)

    def test_substack_url_is_allowed(self):
        # Real third-party newsletters should pass through.
        header = (
            "<https://substack.com/api/v1/email/notification/unsubscribe?token=xyz>"
        )
        assert not _is_internal_forwarder_unsub(header)

    def test_external_groups_google_url_is_allowed(self):
        # A Google Group hosted on someone else's Workspace must NOT trigger
        # the guard — your org domain isn't present.
        header = "<https://groups.google.com/a/example.com/group/list/subscribe>"
        assert not _is_internal_forwarder_unsub(header)

    def test_empty_header_is_not_treated_as_internal(self):
        assert not _is_internal_forwarder_unsub("")

    def test_url_with_org_domain_in_query_param_is_blocked(self):
        # Defensive: even if your org domain only appears as a parameter,
        # refuse. Better to over-refuse than to lose the forwarder again.
        header = f"<https://example.com/unsub?target=ap@{_ORG}>"
        assert _is_internal_forwarder_unsub(header)
