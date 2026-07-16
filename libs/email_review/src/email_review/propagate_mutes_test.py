"""Tests for propagate_mutes.py — the mute-carries-forward fix.

We don't unit-test the Gmail-API plumbing (covered by smoke runs); instead
we pin the core decision: given a thread's labels, does the propagator
detect a muted sibling and identify the inbox-labeled messages to archive?
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from email_review.gmail_actions import MUTED_LABEL_NAME

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[4]
    / ".agents/skills/email-digest/scripts/propagate_mutes.py"
)

# A stand-in for the resolved "Muted" label id (Gmail assigns a real id at
# runtime; the decision logic just needs a consistent value to match on).
_MUTED_ID = "Label_MUTED"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("propagate_mutes_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["propagate_mutes_under_test"] = m
    spec.loader.exec_module(m)
    return m


class TestMutedLabelName:
    def test_label_name_is_muted(self):
        # Sanity: the app's mute label is a plain Gmail label named "Muted".
        assert MUTED_LABEL_NAME == "Muted"


class TestArchiveDecision:
    """Given a thread's messages, decide whether to propagate the mute."""

    def _check(self, mod, thread_messages, monkeypatch):
        """Drive check_thread() against a synthetic thread."""
        def fake_gmail(method, url, body=None):
            # Only the threads.get URL is called from check_thread.
            assert "/threads/" in url
            return {"messages": thread_messages}
        monkeypatch.setattr(mod, "gmail", fake_gmail)
        return mod.check_thread("t1", _MUTED_ID)

    def test_no_muted_sibling_means_no_archive(self, mod, monkeypatch):
        thread = [
            {"id": "m1", "labelIds": ["INBOX"]},
            {"id": "m2", "labelIds": ["INBOX", "UNREAD"]},
        ]
        tid, is_muted, archive = self._check(mod, thread, monkeypatch)
        assert is_muted is False
        assert archive == []

    def test_muted_sibling_with_no_inbox_followups(self, mod, monkeypatch):
        # The pre-existing-muted case: original is muted+archived, no
        # follow-ups in inbox. Nothing to do.
        thread = [
            {"id": "m1", "labelIds": [_MUTED_ID]},  # muted + archived
        ]
        tid, is_muted, archive = self._check(mod, thread, monkeypatch)
        assert is_muted is True
        assert archive == []

    def test_muted_sibling_with_inbox_followup_is_archived(self, mod, monkeypatch):
        # The exact bug: original message muted, follow-up arrived later
        # with fresh INBOX label.
        thread = [
            {"id": "m1", "labelIds": [_MUTED_ID]},
            {"id": "m2_followup", "labelIds": ["INBOX", "UNREAD"]},
        ]
        tid, is_muted, archive = self._check(mod, thread, monkeypatch)
        assert is_muted is True
        assert archive == ["m2_followup"]

    def test_multiple_followups_all_archived(self, mod, monkeypatch):
        thread = [
            {"id": "m1", "labelIds": [_MUTED_ID]},
            {"id": "m2", "labelIds": ["INBOX"]},
            {"id": "m3", "labelIds": ["INBOX", "UNREAD"]},
        ]
        tid, is_muted, archive = self._check(mod, thread, monkeypatch)
        assert is_muted is True
        assert set(archive) == {"m2", "m3"}

    def test_muted_message_itself_with_inbox_is_caught(self, mod, monkeypatch):
        # Defensive: if a single message ever ended up both muted AND with
        # INBOX (race conditions), we should still archive it. The
        # propagator is the safety net.
        thread = [
            {"id": "m1", "labelIds": [_MUTED_ID, "INBOX"]},
        ]
        tid, is_muted, archive = self._check(mod, thread, monkeypatch)
        assert is_muted is True
        assert archive == ["m1"]
