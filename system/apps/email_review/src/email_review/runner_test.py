"""Unit tests for the renderer.

The page crashed in production because some inbox messages have RFC 2822 date
headers without timezone offsets, which made `parsedate_to_datetime` return
naive datetimes that couldn't be sorted against the timezone-aware fallback.
These tests pin the contract: parse_date always returns aware, and rendering
the full page against realistic data never raises.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from email_review import runner


@pytest.fixture
def make_message():
    def _make(**overrides):
        base = {
            "id": "m1",
            "threadId": "t1",
            "labels": ["INBOX"],
            "from": "Alice <alice@example.com>",
            "from_addr": "alice@example.com",
            "to": "you@yourcompany.example",
            "cc": "",
            "subject": "test",
            "date": "Thu, 15 May 2026 12:00:00 +0000",
            "snippet": "hi",
            "synthesis": "Alice — test message",
            "final_bucket": "1",
            "final_why": "test fixture",
            "cold_check": "warm (prior outbound)",
        }
        base.update(overrides)
        return base

    return _make


class TestParseDate:
    """parse_date must always return a timezone-aware datetime."""

    def test_aware_input_returned_as_is(self):
        result = runner.parse_date("Thu, 15 May 2026 12:00:00 +0000")
        assert result.tzinfo is not None

    def test_naive_input_gets_utc_attached(self):
        # The bug: parsedate_to_datetime returns naive for headers like this.
        result = runner.parse_date("Thu, 15 May 2026 12:00:00")
        assert result.tzinfo is not None, "naive datetime must be promoted to aware"

    def test_empty_string_returns_epoch(self):
        result = runner.parse_date("")
        assert result == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_garbage_input_returns_epoch(self):
        result = runner.parse_date("not a date")
        assert result == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_aware_and_naive_inputs_are_mutually_sortable(self):
        # This is the regression: sorting a mix of aware and naive blew up.
        a = runner.parse_date("Thu, 15 May 2026 12:00:00")
        b = runner.parse_date("Thu, 15 May 2026 12:00:00 +0000")
        c = runner.parse_date("")
        sorted([a, b, c])  # must not raise


class TestIndex:
    """The index endpoint must render the full page without raising."""

    def test_empty_inbox(self, tmp_path, monkeypatch, make_message):
        data_file = tmp_path / "data.json"
        data_file.write_text(json.dumps({"stats": {}, "messages": []}))
        monkeypatch.setattr(runner, "DATA_PATH", data_file)
        client = TestClient(runner.app)
        response = client.get("/")
        assert response.status_code == 200

    def test_renders_before_the_classifier_has_ever_run(self, tmp_path, monkeypatch):
        # A freshly adopted workspace has no data.json at all until the
        # email-digest skill runs once. That is the normal empty state, so the
        # page must render rather than 500 on the missing file.
        monkeypatch.setattr(runner, "DATA_PATH", tmp_path / "does_not_exist.json")
        client = TestClient(runner.app)
        response = client.get("/")
        assert response.status_code == 200, response.text

    def test_bucket_fragment_before_the_classifier_has_ever_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "DATA_PATH", tmp_path / "does_not_exist.json")
        client = TestClient(runner.app)
        response = client.get("/api/buckets")
        assert response.status_code == 200, response.text
        assert response.json()["total_msgs"] == 0

    def test_handles_messages_with_mixed_date_formats(
        self, tmp_path, monkeypatch, make_message
    ):
        # Regression for the timezone-naive-vs-aware sort crash.
        data_file = tmp_path / "data.json"
        msgs = [
            make_message(id="m1", date="Thu, 15 May 2026 12:00:00 +0000"),
            make_message(id="m2", date="Thu, 15 May 2026 13:00:00", threadId="t2"),
            make_message(id="m3", date="", threadId="t3"),
        ]
        data_file.write_text(json.dumps({"stats": {}, "messages": msgs}))
        monkeypatch.setattr(runner, "DATA_PATH", data_file)
        client = TestClient(runner.app)
        response = client.get("/")
        assert response.status_code == 200, response.text

    def test_groups_messages_by_thread(self, tmp_path, monkeypatch, make_message):
        data_file = tmp_path / "data.json"
        # Three messages in two threads.
        msgs = [
            make_message(id="m1", threadId="t1", subject="Original"),
            make_message(id="m2", threadId="t1", subject="Re: Original"),
            make_message(id="m3", threadId="t2", subject="Different thread"),
        ]
        data_file.write_text(json.dumps({"stats": {}, "messages": msgs}))
        monkeypatch.setattr(runner, "DATA_PATH", data_file)
        # Isolate the starred/saved sections too, otherwise real on-disk
        # data/.apps/email-review JSONL data leaks into the rendered page and
        # inflates the thread count.
        monkeypatch.setattr(runner, "SAVED_PATH", tmp_path / "saved.jsonl")
        monkeypatch.setattr(runner, "STARRED_PATH", tmp_path / "starred.jsonl")
        client = TestClient(runner.app)
        body = client.get("/").text
        # Two thread entries in the same bucket: one row per threadId.
        # (Element changed from <details> to <div class="thread"> when we
        # added the archive button alongside the toggle.)
        assert body.count('<div class="thread"') == 2

    def test_smoke_renders_realistic_fixture(self, monkeypatch):
        """The bug that caused the 500 was a date-format edge case in real
        Gmail data. This smoke test pins it: load a synthetic fixture covering
        the bucket variety and the timezone-naive-vs-aware date mix, and
        require the page to render. Always runs — no `pytest.skip` — so CI
        catches regressions.
        """
        fixture = (
            Path(__file__).parent / "test_data" / "sample_data.json"
        )
        monkeypatch.setattr(runner, "DATA_PATH", fixture)
        client = TestClient(runner.app)
        response = client.get("/")
        assert response.status_code == 200, response.text[:500]
        # Sanity: enough messages got through that the smoke test is meaningful.
        n_msgs = len(json.loads(fixture.read_text()).get("messages", []))
        assert n_msgs > 10, f"fixture only has {n_msgs} messages — expand it"

    def test_live_data_renders_when_present(self, monkeypatch):
        """If the live data file is present, rendering it must not 500 either.
        Complements the fixture-based smoke test: catches issues in fields the
        fixture redacts (sender/subject content, label combinations on real
        messages).
        """
        live = runner.DATA_PATH
        if not live.exists():
            pytest.skip("no live data to test against (skip in CI / fresh clone)")
        monkeypatch.setattr(runner, "DATA_PATH", live)
        client = TestClient(runner.app)
        response = client.get("/")
        assert response.status_code == 200, response.text[:500]


class TestHealth:
    def test_health_returns_ok(self):
        client = TestClient(runner.app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


class TestExtractMessageBody:
    """The on-expand lazy body loader decodes Gmail `format=full` payloads."""

    def test_prefers_plain_text_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("Hello,\n\nThe full body.\n")}},
                {"mimeType": "text/html", "body": {"data": _b64url("<p>html version</p>")}},
            ],
        }
        text, mime = runner.extract_message_body(payload)
        assert mime == "text/plain"
        assert "The full body." in text and "html version" not in text

    def test_html_only_falls_back_and_strips_script_style(self):
        # No text/plain part -> strip the HTML to text, dropping script/style.
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url("<style>a{color:red}</style><div>Hi</div><div>There</div><script>x()</script>")},
        }
        text, mime = runner.extract_message_body(payload)
        assert mime == "text/html"
        assert "Hi" in text and "There" in text
        assert "color:red" not in text and "x()" not in text

    def test_nested_multipart_recursion(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("nested body")}},
                ]},
                {"mimeType": "application/pdf", "body": {"attachmentId": "abc"}},
            ],
        }
        text, mime = runner.extract_message_body(payload)
        assert mime == "text/plain" and text == "nested body"

    def test_no_text_parts_returns_empty(self):
        payload = {"mimeType": "application/octet-stream", "body": {}}
        text, mime = runner.extract_message_body(payload)
        assert text == "" and mime == "none"

    def test_decode_handles_missing_padding(self):
        # urlsafe base64 without padding must still decode.
        raw = "Body without padding!"
        unpadded = _b64url(raw).rstrip("=")
        payload = {"mimeType": "text/plain", "body": {"data": unpadded}}
        text, _ = runner.extract_message_body(payload)
        assert text == raw


class TestRenderMsgLinks:
    """Each expanded message links out to Gmail."""

    def test_has_gmail_link(self):
        html = runner.render_msg({"id": "m1", "threadId": "t1", "from": "A <a@b.com>", "subject": "s"})
        assert "https://mail.google.com/mail/u/0/#all/m1" in html
        assert "open in Gmail" in html
        # Gmail is the only deep-link the published app renders.
        assert html.lower().count("open in ") == 1


class TestExtractMessageHtml:
    """The expanded view renders the body as sanitized HTML so links work."""

    def test_prefers_html_part_and_keeps_links(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("plain https://x.test/p")}},
                {"mimeType": "text/html", "body": {"data": _b64url(
                    '<p>Hello <a href="https://example.com/welcome">click here</a></p>'
                )}},
            ],
        }
        body, mime = runner.extract_message_html(payload)
        assert mime == "text/html"
        # The anchor (text + href) survives so the link is clickable.
        assert '<a href="https://example.com/welcome"' in body
        assert "click here" in body
        # The plain-text alternative is not used when HTML is present.
        assert "plain https" not in body

    def test_strips_script_and_event_handlers(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                '<div onclick="steal()">Hi</div>'
                '<a href="javascript:evil()">bad</a>'
                "<script>pwn()</script>"
            )},
        }
        body, mime = runner.extract_message_html(payload)
        assert mime == "text/html"
        assert "Hi" in body
        # No script content, no inline handler, no javascript: scheme survives.
        assert "pwn()" not in body
        assert "onclick" not in body
        assert "javascript:" not in body

    def test_plaintext_only_linkifies_bare_urls(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64url("See https://example.com/page for details")},
        }
        body, mime = runner.extract_message_html(payload)
        assert mime == "text/plain"
        assert '<a href="https://example.com/page">https://example.com/page</a>' in body

    def test_plaintext_is_escaped_not_injected(self):
        # A plain-text body containing markup must be escaped, never rendered.
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64url("<script>alert(1)</script> hi")},
        }
        body, _ = runner.extract_message_html(payload)
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_no_text_parts_returns_empty(self):
        payload = {"mimeType": "application/octet-stream", "body": {}}
        body, mime = runner.extract_message_html(payload)
        assert body == "" and mime == "none"


class TestThreadRendering:
    """A thread row renders its actions without any draft/reply affordances
    (the published app is read-and-triage only, no reply compose)."""

    def test_no_draft_or_reply_affordances(self, make_message):
        html = runner.render_thread("t1", [make_message()], "1")
        assert "draft" not in html.lower()
        assert "action--reply" not in html
        assert "reply-compose" not in html
        # The core row controls are still present.
        assert "toggleThread(this)" in html
        assert "action--ask" in html


class TestBucketOverrides:
    """Manual per-thread moves are applied on top of the classifier and survive
    re-loads (re-applied every render)."""

    def test_apply_overrides_rebuckets_matching_threads(self, make_message):
        msgs = [
            make_message(threadId="t1", final_bucket="6"),
            make_message(threadId="t2", final_bucket="3"),
        ]
        runner.apply_bucket_overrides(msgs, {"t1": "1"})
        assert msgs[0]["final_bucket"] == "1"   # moved
        assert msgs[1]["final_bucket"] == "3"   # untouched

    def test_apply_overrides_empty_is_noop(self, make_message):
        m = make_message(threadId="t1", final_bucket="6")
        runner.apply_bucket_overrides([m], {})
        assert m["final_bucket"] == "6"

    def test_build_move_record_captures_labeled_signal(self, make_message):
        msgs = [make_message(
            **{"from": "Andrew <a@tiny.com>", "from_addr": "a@tiny.com",
               "subject": "Interesting People", "snippet": "come to victoria",
               "final_bucket": "1", "final_why": "looks personal", "date": "Thu, 15 May 2026 12:00:00 +0000"}
        )]
        rec = runner.build_move_record("t1", "3", "1", msgs, "2026-06-29T00:00:00+00:00")
        assert rec["from_addr"] == "a@tiny.com"
        assert rec["subject"] == "Interesting People"
        assert rec["snippet"] == "come to victoria"
        assert rec["classifier_bucket"] == "1"          # what the classifier chose
        assert rec["classifier_why"] == "looks personal"
        assert rec["from_bucket"] == "1" and rec["to_bucket"] == "3"
        assert rec["to_bucket_name"] == "FYI / read"
        assert rec["moved_at"] == "2026-06-29T00:00:00+00:00"

    def test_move_options_exclude_current_bucket(self):
        html = runner._move_options_html("1")
        assert "move…" in html
        assert 'value="1"' not in html          # can't move to where it already is
        assert 'value="3"' in html              # other buckets offered
        assert "FYI / read" in html             # options carry bucket names


def _cfemail(addr: str, key: int = 0x42) -> str:
    """Encode an address the way Cloudflare does, for the decode tests."""
    return bytes([key, *(ord(ch) ^ key for ch in addr)]).hex()


class TestCloudflareEmailDecoding:
    """Cloudflare obfuscates addresses as `[email protected]`; we decode them
    back to real mailto links before rendering."""

    def test_decodes_obfuscated_span_to_mailto(self):
        hexed = _cfemail("hello@example.com")
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                f'<p>Reach <span class="__cf_email__" data-cfemail="{hexed}">'
                "[email&#160;protected]</span> anytime</p>"
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert 'href="mailto:hello@example.com"' in body
        assert ">hello@example.com</a>" in body
        assert "[email" not in body

    def test_rewrites_protection_href_keeping_link_text(self):
        hexed = _cfemail("sales@example.com")
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                f'<a href="/cdn-cgi/l/email-protection#{hexed}">Contact sales</a>'
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert 'href="mailto:sales@example.com"' in body
        assert "Contact sales" in body

    def test_decode_helper_round_trips(self):
        assert runner._decode_cfemail(_cfemail("a.b+c@sub.example.org")) == "a.b+c@sub.example.org"

    def test_malformed_hex_left_untouched(self):
        # Odd-length / non-decodable hex must not crash and must not produce a link.
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                '<span class="__cf_email__" data-cfemail="abc">[email&#160;protected]</span>'
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert "mailto:" not in body


class TestTrackingPixels:
    """1x1 tracking pixels are stripped; real remote images are kept."""

    def test_strips_attribute_sized_pixel(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                '<p>Hi</p><img src="https://track.test/open?id=1" width="1" height="1">'
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert "track.test" not in body

    def test_strips_style_sized_pixel(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                '<img src="https://track.test/p.gif" style="width:1px;height:1px;border:0">'
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert "track.test" not in body

    def test_keeps_real_remote_image(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(
                '<img src="https://cdn.test/logo.png" width="200" height="60" alt="Logo">'
            )},
        }
        body, _ = runner.extract_message_html(payload)
        assert "https://cdn.test/logo.png" in body

    def test_keeps_image_with_no_declared_size(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url('<img src="https://cdn.test/banner.jpg" alt="Banner">')},
        }
        body, _ = runner.extract_message_html(payload)
        assert "https://cdn.test/banner.jpg" in body
