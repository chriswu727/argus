"""Shared pytest fixtures for Argus detector tests.

These tests exercise pure-Python logic — no browser, no LLM. Detectors
are run against synthetic PageState / error / page-content payloads.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from argus.access import AccessPolicy
from argus.application.context import runtime_scope
from argus.bootstrap import create_runtime
from argus.detector import Detector
from argus.entrypoints.mcp.server import create_server
from argus.models import InteractiveElement, PageState
from argus.settings import Settings


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    import argus.mcp_server as m

    monkeypatch.setenv("ARGUS_FILE_ROOTS", json.dumps([str(tmp_path)]))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runtime = create_runtime(
        Settings(output_dir=tmp_path / "reports", access=AccessPolicy(file_roots=(tmp_path,)))
    )
    monkeypatch.setattr(m, "runtime", runtime)
    monkeypatch.setattr(m, "mcp", create_server(runtime, "full"))
    with runtime_scope(runtime):
        yield runtime


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("ARGUS_REQUIRE_BROWSER") == "1" and report.skipped:
        reason = str(report.longrepr).lower()
        if "chromium" in reason or "browser" in reason:
            report.outcome = "failed"
            report.longrepr = f"Required browser test was skipped: {report.longrepr}"


@pytest.fixture
def detector() -> Detector:
    """Fresh Detector with empty `_seen` cache. Most tests want isolation."""
    return Detector()


@pytest.fixture
def empty_steps() -> list:
    return []


def make_page_state(
    url: str = "http://example.test/",
    title: str = "Test Page",
    page_text: str = "",
    elements: list | None = None,
    toast_messages: list | None = None,
    counts: dict | None = None,
    css_indicators: list | None = None,
    item_lists: dict | None = None,
    images: list | None = None,
    meta_tags: dict | None = None,
    headings: list | None = None,
    accessibility_issues: list | None = None,
    mixed_content: list | None = None,
) -> PageState:
    """Build a PageState with sensible defaults — keeps test bodies short."""
    return PageState(
        url=url,
        title=title,
        elements=elements or [],
        page_text=page_text,
        toast_messages=toast_messages or [],
        counts=counts or {},
        css_indicators=css_indicators or [],
        item_lists=item_lists or {},
        links=[],
        images=images or [],
        meta_tags=meta_tags or {},
        headings=headings or [],
        accessibility_issues=accessibility_issues or [],
        mixed_content=mixed_content or [],
    )


def make_element(
    index: int = 0,
    tag: str = "button",
    text: str | None = None,
    type: str | None = None,
    name: str | None = None,
    id: str | None = None,
    placeholder: str | None = None,
    value: str | None = None,
) -> InteractiveElement:
    return InteractiveElement(
        index=index,
        tag=tag,
        text=text,
        type=type,
        name=name,
        id=id,
        placeholder=placeholder,
        value=value,
    )


@pytest.fixture
def page_factory():
    """Convenience factory exposed as a fixture."""
    return make_page_state


@pytest.fixture
def element_factory():
    return make_element
