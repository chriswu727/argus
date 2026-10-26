from __future__ import annotations

from typing_extensions import TypedDict


class FindingEvidence(TypedDict, total=False):
    description: str | None
    steps: list[str] | str | None
    url: str | None
    screenshot: str | None
    bug_type: str | None
    expected: str
    actual: str
    goal: str | list[str]
    goals: list[str]
    evidence_calls: list[int]
    target_text: str | None
    target: str | None
    expect: str | None
    at_url: str | None
    after_url: str | None
    replay: bool | None
    expect_status: int | None
