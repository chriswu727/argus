"""Detector tests for process_network_errors."""
from __future__ import annotations


def test_5xx_is_high_severity(detector, empty_steps):
    bugs = detector.process_network_errors(
        [{"method": "POST", "url": "http://example.test/api/save", "status": 500}],
        url="http://example.test/",
        steps=empty_steps,
    )
    assert len(bugs) == 1
    assert bugs[0].severity.value == "high"
    assert "500" in bugs[0].title


def test_4xx_is_medium_severity(detector, empty_steps):
    bugs = detector.process_network_errors(
        [{"method": "GET", "url": "http://example.test/missing", "status": 404}],
        url="http://example.test/",
        steps=empty_steps,
    )
    assert len(bugs) == 1
    assert bugs[0].severity.value == "medium"


def test_network_errors_dedup(detector, empty_steps):
    err = {"method": "GET", "url": "http://example.test/x", "status": 404}
    bugs1 = detector.process_network_errors([err], "http://example.test/", empty_steps)
    bugs2 = detector.process_network_errors([err], "http://example.test/", empty_steps)
    assert len(bugs1) == 1
    assert len(bugs2) == 0
