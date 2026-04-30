"""Detector tests for process_console_errors."""
from __future__ import annotations


def test_console_exception_high_severity(detector, empty_steps):
    bugs = detector.process_console_errors(
        [{"type": "exception", "text": "ReferenceError: appConfig is not defined"}],
        url="http://example.test/",
        steps=empty_steps,
    )
    assert len(bugs) == 1
    assert bugs[0].severity.value == "high"
    assert "appConfig" in bugs[0].title
    assert "exception" in bugs[0].title.lower()


def test_console_error_medium_severity(detector, empty_steps):
    bugs = detector.process_console_errors(
        [{"type": "error", "text": "Failed to load resource"}],
        url="http://example.test/",
        steps=empty_steps,
    )
    assert len(bugs) == 1
    assert bugs[0].severity.value == "medium"


def test_console_dedup_same_text_in_same_session(detector, empty_steps):
    payload = [{"type": "error", "text": "X is undefined"}]
    bugs1 = detector.process_console_errors(payload, "http://a/", empty_steps)
    bugs2 = detector.process_console_errors(payload, "http://b/", empty_steps)
    assert len(bugs1) == 1
    assert len(bugs2) == 0  # already seen


def test_console_warnings_aggregated_by_pattern(detector, empty_steps):
    """Warnings differing only by URL are collapsed into one aggregate Bug."""
    warnings = [
        {"type": "warning", "text": "Resource https://a.test/foo.png was preloaded"},
        {"type": "warning", "text": "Resource https://a.test/bar.png was preloaded"},
        {"type": "warning", "text": "Resource https://b.test/baz.png was preloaded"},
    ]
    bugs = detector.process_console_errors(warnings, "http://example.test/", empty_steps)
    assert len(bugs) == 1
    assert bugs[0].severity.value == "low"
    assert "3" in bugs[0].title or "warning" in bugs[0].title.lower()


def test_console_empty_input_yields_no_bugs(detector, empty_steps):
    assert detector.process_console_errors([], "http://example.test/", empty_steps) == []
