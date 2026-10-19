from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.access import AccessPolicy, canonical_origin
from argus.adapters.reporting.html import render_html
from argus.adapters.reporting.json import render_json
from argus.adapters.reporting.junit import render_junit
from argus.adapters.reporting.sarif import render_sarif
from argus.application.findings import record_bug, record_observation
from argus.application.network_storage import cookies_get, storage_get
from argus.application.screenshots import screenshot_diff
from argus.browser import BrowserDriver
from argus.domain.contracts import ErrorCode, OperationError
from argus.domain.models import Bug, BugType, ExplorationResult, Severity
from argus.security import _redact, _redact_headers, redact_body
from argus.settings import Settings


@pytest.mark.parametrize(
    "value",
    [
        {"password": 'prefix"SYNTHETIC_SECRET'},
        {"access_token": ["SYNTHETIC_SECRET"]},
        {"nested": {"secret": {"value": "SYNTHETIC_SECRET"}}},
        [{"password": "SYNTHETIC_SECRET", "name": "Ada"}],
    ],
)
def test_structured_credentials_are_redacted_without_invalidating_json(value):
    result = _redact(json.dumps(value))
    assert "SYNTHETIC_SECRET" not in result
    assert json.loads(result)


@pytest.mark.parametrize(
    "text",
    [
        '{"pass\\u0077ord":"SYNTHETIC_SECRET"}',
        '{"password":"SYNTHETIC_SECRET',
        "https://example.test/?access%5ftoken=SYNTHETIC_SECRET&name=Ada",
        "https://user:SYNTHETIC_SECRET@example.test/",
        'Content-Disposition: form-data; name="password"\r\n\r\nSYNTHETIC_SECRET',
    ],
)
def test_encoded_and_malformed_credentials_fail_closed(text):
    assert "SYNTHETIC_SECRET" not in _redact(text)


@pytest.mark.parametrize(
    "text",
    ["[P1] Session expires after save", "[Auth] Login failed", "{UI} Password field is clipped"],
)
def test_bracket_prefixed_prose_is_not_destroyed(text):
    assert _redact(text) == text


def test_malformed_json_with_escaped_secret_key_is_redacted():
    assert "SYNTHETIC_SECRET" not in _redact('{"pass\\u0077ord":"SYNTHETIC_SECRET')


def test_multipart_and_custom_credential_headers_are_not_retained():
    assert "SYNTHETIC_SECRET" not in redact_body(
        "SYNTHETIC_SECRET", "multipart/form-data; boundary=x"
    )
    result = _redact_headers({"X-Service-Token": "SYNTHETIC_SECRET", "X-Request-ID": "trace-123"})
    assert result == {"X-Service-Token": "[redacted]", "X-Request-ID": "trace-123"}


@pytest.mark.parametrize("render", [render_html, render_json, render_junit, render_sarif])
def test_every_report_format_redacts_urls_and_nested_evidence_without_mutation(render):
    url = "https://example.test/reset?token=SYNTHETIC_SECRET"
    bug = Bug(
        BugType.UX_ISSUE,
        Severity.HIGH,
        "Synthetic finding",
        "Evidence",
        url,
        [url],
        network_logs=[{"url": url, "method": "GET", "status": 200}],
        reproduction_receipt={"attempted": True, "reproduced": True, "at_url": url},
    )
    result = ExplorationResult(url, [bug], [url], 0, 0, [])
    exported = render(result)
    assert "SYNTHETIC_SECRET" not in exported
    assert result.url == url and bug.url == url
    assert result.bugs[0].reproduction_receipt["at_url"] == url


def test_captured_request_urls_and_multipart_are_redacted():
    driver = BrowserDriver()
    request = SimpleNamespace(
        url="https://example.test/?token=SYNTHETIC_SECRET",
        method="POST",
        resource_type="xhr",
        frame=SimpleNamespace(url="https://example.test/?password=SYNTHETIC_SECRET"),
        headers={"content-type": "multipart/form-data; boundary=x"},
        post_data="SYNTHETIC_SECRET",
    )
    driver._on_request(request)
    assert "SYNTHETIC_SECRET" not in str(driver.network_log[-1])


@pytest.mark.parametrize("url", ["https://example.test:443/", "https://EXAMPLE.test/path"])
def test_origin_normalization(url):
    assert canonical_origin(url) == canonical_origin("https://example.test")


@pytest.mark.parametrize(
    "target", ["http://example.test/app", "https://other.test/app", "about:blank"]
)
async def test_capsules_never_apply_storage_after_origin_changes(target):
    class Browser(BrowserDriver):
        def __init__(self):
            super().__init__()
            self._page = SimpleNamespace(url="about:blank")
            self.writes = []

        async def cookies_clear(self):
            return True

        async def goto(self, url):
            self._page.url = target

        async def storage_clear(self, kind):
            self.writes.append(kind)

        async def storage_set(self, key, value, kind):
            self.writes.append(value)

    browser = Browser()
    result = await browser.capsule_apply(
        {"url": "https://example.test/app", "local": {"token": "SYNTHETIC_SECRET"}}
    )
    assert result["origin_ok"] is False
    assert browser.writes == []


def test_policy_rejects_unapproved_origins_and_schemes():
    policy = AccessPolicy(allowed_origins=("http://localhost:3000",))
    policy.check_url("http://localhost:3000/app")
    for url in [
        "http://localhost:3001",
        "https://localhost:3000",
        "javascript:alert(1)",
        "data:text/plain,x",
        "https://u:p@localhost:3000",
    ]:
        with pytest.raises(OperationError):
            policy.check_url(url)


def test_file_policy_resolves_symlinks_and_denies_by_default(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    inside = approved / "fixture.txt"
    inside.write_text("synthetic")
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic")
    link = approved / "linked.txt"
    link.symlink_to(outside)
    policy = AccessPolicy(file_roots=(approved,))
    assert policy.check_file(inside) == inside
    policy.check_url(inside.as_uri())
    for path in [outside, link, approved / ".." / "outside.txt"]:
        with pytest.raises(OperationError):
            policy.check_file(path)
    with pytest.raises(OperationError):
        AccessPolicy().check_url(inside.as_uri())


@pytest.mark.parametrize(
    "env",
    [
        {"ARGUS_ALLOWED_ORIGINS": "not-json"},
        {"ARGUS_ALLOWED_ORIGINS": "null"},
        {"ARGUS_ALLOWED_ORIGINS": '["https://example.test/path"]'},
        {"ARGUS_FILE_ROOTS": "null"},
        {"ARGUS_FILE_ROOTS": "[1]"},
    ],
)
def test_invalid_startup_policy_is_rejected(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


async def test_upload_and_drop_deny_before_touching_browser(tmp_path):
    path = tmp_path / "fake.txt"
    path.write_text("synthetic")
    browser = BrowserDriver(access_policy=AccessPolicy())
    with pytest.raises(OperationError):
        await browser.upload_file(0, [str(path)], [])
    with pytest.raises(OperationError):
        await browser.drop_file(0, str(path), [])


async def test_sensitive_reads_require_startup_opt_in(isolated_runtime):
    class Browser:
        has_page = True
        current_url = "https://example.test/"

        async def cookies_get(self, url=None):
            return [{"name": "opaque", "value": "SYNTHETIC_SECRET"}]

        async def storage_get(self, kind):
            return {"opaque": "SYNTHETIC_SECRET"}

    isolated_runtime.session.browser = Browser()
    isolated_runtime.session.mode = "web"
    for tool in [cookies_get, storage_get]:
        assert "SYNTHETIC_SECRET" not in await tool()
    isolated_runtime.settings = replace(
        isolated_runtime.settings, access=AccessPolicy(sensitive_reads=True)
    )
    for tool in [cookies_get, storage_get]:
        assert "SYNTHETIC_SECRET" in await tool()


async def test_runtime_blocks_operations_after_unapproved_navigation(isolated_runtime):
    runtime = isolated_runtime
    runtime.settings = replace(
        runtime.settings, access=AccessPolicy(allowed_origins=("https://approved.test",))
    )
    runtime.session.browser = SimpleNamespace(current_url="https://unapproved.test", has_page=True)
    called = False

    async def action():
        nonlocal called
        called = True

    result = await runtime.execute("click_what", action)
    assert result.error == ErrorCode.PERMISSION and not called


async def test_evidence_tools_cannot_import_unapproved_files(isolated_runtime, tmp_path):
    path = tmp_path / "external.png"
    path.write_bytes(b"synthetic-not-an-image")
    runtime = isolated_runtime
    runtime.settings = replace(runtime.settings, access=AccessPolicy())
    runtime.session.browser = SimpleNamespace(current_url="https://example.test", has_page=True)
    with pytest.raises(OperationError):
        await screenshot_diff(str(path), str(path))
    with pytest.raises(OperationError):
        await record_bug("Synthetic", "low", {"screenshot": str(path)})
    with pytest.raises(OperationError):
        await record_observation("Synthetic", "Evidence", screenshot=str(path))


def test_container_only_copies_packaging_inputs():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY . ." not in dockerfile
    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile
    assert "COPY argus/ ./argus/" in dockerfile
    assert "'pip>=26.2.1' 'setuptools>=83.0.0'" in dockerfile
    patterns = (root / ".dockerignore").read_text().splitlines()
    assert {".env", ".env.*", "**/.env", "**/.env.*", ".argus", "**/.argus"} <= set(patterns)


def test_workflows_pin_actions_and_gate_on_dependency_audit():
    root = Path(__file__).resolve().parents[1]
    for name in ["ci.yml", "release.yml"]:
        workflow = (root / ".github" / "workflows" / name).read_text()
        actions = re.findall(r"uses:\s+([^\s]+)", workflow)
        assert actions and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in actions)
        assert "python -m pip_audit --vulnerability-service osv" in workflow
        assert "--ignore-vuln" not in workflow
        assert "setuptools>=83.0.0" in workflow
    release = (root / ".github/workflows/release.yml").read_text()
    assert "/releases/latest/" not in release
    assert release.index("sha256sum --check") < release.index("tar xzf mcp-publisher.tar.gz")


async def test_link_probes_do_not_follow_redirects_or_mislabel_policy_denials():
    calls = []

    async def head(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=302, ok=False)

    browser = BrowserDriver(access_policy=AccessPolicy(allowed_origins=("https://approved.test",)))
    browser._context = SimpleNamespace(request=SimpleNamespace(head=head))
    result = await browser.check_links(
        [
            {"href": "https://approved.test/redirect", "isInternal": True},
            {"href": "https://unapproved.test/", "isInternal": True},
        ]
    )
    assert len(calls) == 1 and calls[0][1]["max_redirects"] == 0
    assert [item["ok"] for item in result] == [None, None]


async def test_background_tabs_never_expose_disallowed_metadata():
    class Page:
        def __init__(self, url):
            self.url = url
            self.reads = 0

        def is_closed(self):
            return False

        async def title(self):
            self.reads += 1
            return "Synthetic title"

    approved = Page("https://approved.test/?token=SYNTHETIC_SECRET")
    denied = Page("https://unapproved.test/?token=SYNTHETIC_SECRET")
    browser = BrowserDriver(access_policy=AccessPolicy(allowed_origins=("https://approved.test",)))
    browser._context = SimpleNamespace(pages=[approved, denied])
    browser._page = approved
    tabs = await browser.tabs_list()
    assert "SYNTHETIC_SECRET" not in str(tabs) + str(browser.page_urls())
    assert "unapproved.test" not in str(tabs) + str(browser.page_urls())
    assert tabs[1]["title"] == "" and denied.reads == 0
    with pytest.raises(OperationError):
        await browser.tabs_switch(1)
    assert browser._page is approved


async def test_tab_navigation_during_title_read_cannot_leak_metadata():
    class Page:
        url = "https://approved.test/"

        def is_closed(self):
            return False

        async def title(self):
            self.url = "https://unapproved.test/"
            return "SYNTHETIC_SECRET"

    page = Page()
    browser = BrowserDriver(access_policy=AccessPolicy(allowed_origins=("https://approved.test",)))
    browser._context = SimpleNamespace(pages=[page])
    assert (await browser.tabs_list())[0]["title"] == ""


async def test_capsule_round_trip_for_approved_file_page(tmp_path):
    page = tmp_path / "fixture.html"
    page.write_text("<html><body>Local fixture</body></html>")
    other = tmp_path / "other.html"
    other.write_text("<html><body>Different fixture</body></html>")
    policy = AccessPolicy(file_roots=(tmp_path,))
    assert policy.storage_scope(page.as_uri()) != policy.storage_scope(other.as_uri())
    browser = BrowserDriver(access_policy=policy)
    try:
        await browser.start()
        await browser.goto(page.as_uri())
        await browser.storage_set("state", "saved", "local")
        await browser.storage_set("state", "saved", "session")
        capsule = await browser.capsule_capture()
        await browser.storage_set("state", "changed", "local")
        applied = await browser.capsule_apply(capsule)
        assert applied["origin_ok"] and applied["local"] == 1 and applied["session"] == 1
        assert (await browser.storage_get("local"))["state"] == "saved"
    finally:
        await browser.stop()
