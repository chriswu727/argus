from __future__ import annotations

from types import SimpleNamespace

from argus.browser import BrowserDriver
from argus.runtime import BoundedEventLog


def test_bounded_event_log_preserves_cursor_semantics_after_eviction():
    log = BoundedEventLog[str](2)
    log.append("first")
    cursor = log.cursor()
    log.append("second")
    log.append("third")

    assert list(log) == ["second", "third"]
    assert log.since(cursor) == ["second", "third"]
    assert log.dropped == 1


def test_browser_event_logs_are_bounded(monkeypatch):
    monkeypatch.setenv("ARGUS_MAX_NETWORK_EVENTS", "2")
    driver = BrowserDriver()

    for value in range(3):
        driver.network_log.append({"value": value})

    assert [entry["value"] for entry in driver.network_log] == [1, 2]
    assert driver.network_log.dropped == 1


def test_request_capture_redacts_credentials():
    driver = BrowserDriver()
    request = SimpleNamespace(
        url="https://example.test/api",
        method="POST",
        resource_type="xhr",
        frame=SimpleNamespace(url="https://example.test/"),
        headers={"authorization": "Bearer secret", "content-type": "application/json"},
        post_data='{"password":"hunter2","name":"safe"}',
    )

    driver._on_request(request)
    entry = driver.network_log[-1]

    assert entry["headers"]["authorization"] == "[redacted]"
    assert "hunter2" not in entry["post_data"]
    assert "safe" in entry["post_data"]


async def test_binary_response_body_is_not_loaded():
    driver = BrowserDriver()
    request = SimpleNamespace(resource_type="image", method="GET")
    driver.network_log.append({"id": id(request)})

    class Response:
        status = 200
        url = "https://example.test/hero.png"
        headers = {"content-type": "image/png", "content-length": "5000000"}

        def __init__(self):
            self.request = request

        async def body(self):
            raise AssertionError("binary response body should not be loaded")

    await driver._on_response(Response())
    entry = driver.network_log[-1]

    assert entry["response_size"] == 5_000_000
    assert entry["response_body"] is None
    assert entry["response_body_omitted"]


async def test_api_response_body_is_captured_and_redacted():
    driver = BrowserDriver()
    request = SimpleNamespace(resource_type="fetch", method="GET")
    driver.network_log.append({"id": id(request)})

    class Response:
        status = 200
        url = "https://example.test/api/me"
        headers = {"content-type": "application/json", "content-length": "44"}

        def __init__(self):
            self.request = request

        async def body(self):
            return b'{"access_token":"secret-value","name":"Ada"}'

    await driver._on_response(Response())
    entry = driver.network_log[-1]

    assert "secret-value" not in entry["response_body"]
    assert "Ada" in entry["response_body"]
