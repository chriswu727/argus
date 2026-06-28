"""Unit tests for the oracle substrate: response-body capture + membership diffing."""
from __future__ import annotations

from argus.browser import _capture_body, _redact, _redact_headers
from argus.differ import compute_changes
from tests.conftest import make_page_state


def test_capture_body_keeps_json_and_text():
    assert _capture_body(b'{"error":"nope"}', {"Content-Type": "application/json"}) == '{"error":"nope"}'
    assert _capture_body(b"plain", {"Content-Type": "text/plain"}) == "plain"


def test_capture_body_skips_binary():
    assert _capture_body(b"\x89PNG\r\n\x1a", {"Content-Type": "image/png"}) is None


def test_capture_body_redacts_secrets_keeps_rest():
    b = _capture_body(b'{"token":"abc123","user":"alice"}', {"Content-Type": "application/json"})
    assert "abc123" not in b
    assert "[redacted]" in b
    assert "alice" in b  # non-secret fields survive


def test_capture_body_caps_large_payload():
    b = _capture_body(b"x" * (40 * 1024), {"Content-Type": "text/plain"})
    assert "truncated" in b
    assert len(b) < 40 * 1024


def test_capture_body_empty_is_none():
    assert _capture_body(b"", {"Content-Type": "application/json"}) is None


def test_redact_masks_jwt_anywhere():
    # A JWT leaks regardless of shape — form-encoded, HTML attr, bare.
    jwt = "eyJabc123._payloadpart.sigpart"
    assert jwt not in _redact(f"token={jwt}&x=1")
    assert jwt not in _redact(f'<script>var t="{jwt}"</script>')
    assert "[redacted-jwt]" in _redact(jwt)


def test_redact_masks_form_encoded_and_variant_keys():
    out = _redact("access_token=abc123&user=alice&refresh_token=xyz")
    assert "abc123" not in out and "xyz" not in out
    assert "user=alice" in out  # non-secret survives
    # variant JSON key names (authToken, sessionId) are caught
    assert "secretval" not in _redact('{"authToken":"secretval","sessionId":"sv2"}')


def test_redact_headers_masks_credentials():
    h = _redact_headers({"Cookie": "session=abc", "Authorization": "Bearer xyz", "X-Foo": "bar"})
    assert h["Cookie"] == "[redacted]"
    assert h["Authorization"] == "[redacted]"
    assert h["X-Foo"] == "bar"  # non-credential header untouched


def test_capture_body_redacts_secret_spanning_the_cap():
    # Redaction runs on the full body before truncation, so a value extending
    # past the 16KB cap can't leak its prefix (regression for F1).
    body = ('{"token":"' + "a" * 20000 + '"}').encode()
    out = _capture_body(body, {"Content-Type": "application/json"})
    assert "[redacted]" in out
    assert "aaaaaaaaaa" not in out


def test_differ_reports_duplicate_occurrences():
    # A duplicate-row bug: set-based diffing would hide it; multiset shows (x2).
    before = make_page_state(item_lists={"rows": ["A", "B"]})
    after = make_page_state(item_lists={"rows": ["A", "B", "B", "B"]})
    joined = "\n".join(compute_changes(before, after))
    assert "gained: 'B' (x2)" in joined


def test_differ_reports_list_membership_not_just_length():
    # A swap keeps the count identical but changes membership — length-only
    # diffing (the old behaviour) would report nothing.
    before = make_page_state(item_lists={"tasks": ["Buy milk", "Pay rent"]})
    after = make_page_state(item_lists={"tasks": ["Pay rent", "Walk dog"]})
    joined = "\n".join(compute_changes(before, after))
    assert "removed: 'Buy milk'" in joined
    assert "gained: 'Walk dog'" in joined
