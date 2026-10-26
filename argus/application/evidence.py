from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from argus.application.context import current_runtime
from argus.domain.models import (
    Bug,
    Screenshot,
)
from argus.security import _redact
from argus.storage import (
    StorageError,
    storage_warning,
)

from .coverage import _coverage_reference_url, _current_coverage_url
from .session import Session
from .text import _short


def _record_action(
    s: "Session", tool: str, description: str = "", value: Optional[str] = None
) -> None:
    """Append a structured, replayable action to the session trace."""
    action = {"tool": tool, "description": description, "value": value}
    if tool in {"type_into", "paste_into", "select_into"} and value is not None:
        action = {"tool": tool, "description": description, "value_ref": s.inputs.store(value)}
    s.action_trace.append(action)
    _record_coverage_action(s, tool, description)


def _record_coverage_action(s: "Session", tool: str, description: str = "") -> None:
    if tool == "paste_into":
        description = "field input (value omitted)"
    s.coverage_action_trace.append(
        {
            "tool": tool,
            "description": _short(_redact(description), 120),
            "url": _coverage_reference_url(_current_coverage_url(s)),
        }
    )


def _output_dir() -> str:
    return str(current_runtime().settings.output_dir)


def _screenshot_dir(s: "Session") -> Path:
    return Path(_output_dir()) / "screenshots" / s.run_id


def _journal_path(origin: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", origin or "default")
    return Path(_output_dir()) / ".argus" / "journal" / f"{safe}.json"


def _bug_fingerprint(bug: "Bug") -> str:
    """Stable identity for cross-run dedup — from the receipt's structural fields
    (type + url path + expect + target), never the LLM-authored title, so a
    genuinely new bug is never silently collapsed into an old one."""
    r = bug.reproduction_receipt or {}
    path = urlparse(r.get("at_url") or bug.url or "").path or "/"
    return (
        f"{bug.type.value}|{path}|{r.get('expect', '')}|"
        f"{(r.get('target_text') or '')[:80]}|{r.get('expect_status', '')}"
    )


def _journal_entries(origin: str, *, strict: bool = False) -> list:
    path = _journal_path(origin)
    try:
        return current_runtime().journal.read(path)
    except StorageError as exc:
        if strict:
            raise
        storage_warning(str(exc))
        return []


def _write_journal(s: "Session") -> None:
    """Persist re-checkable findings (those with a clean-load verify clause) so a
    later run can re-test them. Replay-mode receipts are skipped — re-driving
    them re-executes writes, so they're not safe to auto-regression-check."""
    origin = urlparse(s.url or "").netloc or "default"
    fresh = []
    for b in s.bugs:
        r = b.reproduction_receipt or {}
        if r.get("mode") == "replay":
            continue
        expect, target = r.get("expect"), r.get("target_text")
        expected_status = r.get("expect_status")
        # Only journal findings Argus actually CONFIRMED (reproduced). A
        # not-reproduced / errored finding would otherwise resurface next run as
        # a phantom "no-longer-reproduces (likely fixed)".
        text_check = expect in ("present", "absent") and bool(target)
        status_check = expected_status is not None
        if r.get("reproduced") is True and (text_check or status_check):
            verify = {"at_url": r.get("at_url", "")}
            if status_check:
                verify["expect_status"] = expected_status
            else:
                verify.update({"expect": expect, "target_text": target})
            fresh.append(
                {
                    "fingerprint": _bug_fingerprint(b),
                    "title": b.title[:120],
                    "severity": b.severity.value,
                    "type": b.type.value,
                    "verify": verify,
                }
            )
    if not fresh:
        return
    path = _journal_path(origin)
    current_runtime().journal.merge(path, fresh)


async def _auto_screenshot(s: Session, name: str, step: str) -> Optional[str]:
    """Take a screenshot and register it in the session.

    Mode-aware: web-mode goes through Playwright; screen-mode shells
    out to `screencapture`. Both produce a PNG at the same output
    location and append a Screenshot record with the correct URL.
    """
    s._screenshot_counter += 1
    safe_name = f"{s._screenshot_counter:03d}_{name}"
    path = str(_screenshot_dir(s) / f"{safe_name}.png")

    if s.mode == "web" and s.browser is not None:
        await s.browser.screenshot(path)
        url = s.browser.current_url
    elif s.mode == "screen" and s.screen is not None:
        # Shell out for the same convention used elsewhere.
        import subprocess as _sp

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            _sp.run(
                ["screencapture", "-t", "png", "-x", path],
                capture_output=True,
                timeout=5,
                check=True,
            )
        except Exception:
            return None
        url = f"screen://{s.screen.app_name or 'unknown'}"
    else:
        return None

    if not Path(path).exists():
        return None

    s.screenshots.append(
        Screenshot(
            path=path,
            name=safe_name,
            step=step,
            url=url,
        )
    )
    return path
