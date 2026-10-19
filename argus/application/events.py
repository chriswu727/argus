from __future__ import annotations

from argus.domain.models import (
    BugType,
    Severity,
)
from argus.security import _redact

from .capture import _file_event_bugs
from .evidence import _auto_screenshot
from .session import _require_session, _require_web_session
from .text import _short


async def get_downloads() -> str:
    """List files DOWNLOADED this session (Export CSV, Download invoice / PDF /
    report) with each file's name, byte size, and a text preview of its contents.

    Downloads are otherwise a BLIND SPOT — observe/click_what/get_errors show
    nothing when a button triggers a download, so a broken export (empty CSV,
    header-only with zero data rows, 0-byte PDF, wrong file) silently passes as if
    it worked. After clicking an export/download control, call this to VERIFY the
    file actually downloaded AND its contents are right, then record_bug with the
    filename+size+preview as your reproduction evidence. A download control that
    fires NO download at all is itself a bug.
    """
    s = _require_session()
    err = _require_web_session(s, "get_downloads")
    if err:
        return err
    dls = await s.browser.downloads_snapshot()
    if not dls:
        return (
            "No downloads captured this session. If you just clicked an "
            "export/download control and nothing arrived here, the download "
            "never fired — that is a bug worth recording."
        )
    lines = [f"{len(dls)} download(s) captured:"]
    for d in dls:
        sz = d.get("size")
        szs = f"{sz} bytes" if sz is not None else "unreadable"
        lines.append(f"  - {d.get('filename') or '(no filename)'} — {szs}  [{d.get('url') or ''}]")
        if sz is not None and sz < 50:
            lines.append(f"    [SUSPICIOUS: only {sz} bytes — likely an empty/broken export]")
        pv = d.get("preview")
        if pv:
            lines.append(f"    preview: {pv[:300]!r}")
        if d.get("error"):
            lines.append(f"    (could not read file: {d['error']})")
    return "\n".join(lines)


async def get_errors() -> str:
    """Drain captured browser events (console errors, HTTP 4xx/5xx) since
    the last call.

    These two channels are not visible to you through page state, so Argus
    captures them as Bug records automatically. Use this after any action
    that might have triggered a JS error or backend failure.

    For everything else (visual issues, copy problems, missed validation,
    cross-page inconsistency), read the page state yourself and call
    record_bug when you've confirmed something is a real bug.
    """
    s = _require_session()
    console_errs, network_errs = s.browser.drain_errors()

    current_url = s.browser.current_url
    recent = s.steps[s._steps_since_last_bug :]
    new_bugs = s.detector.process_console_errors(console_errs, current_url, recent)
    new_bugs.extend(s.detector.process_network_errors(network_errs, current_url, recent))

    if new_bugs:
        # Name the actual error so two error cards on the same URL are
        # distinguishable in the timeline (was a generic "Error detected on URL").
        label = (
            _short(new_bugs[0].title, 70)
            if len(new_bugs) == 1
            else f"{len(new_bugs)} errors on {current_url}"
        )
        ss_path = await _auto_screenshot(s, f"error_{len(s.bugs) + 1}", label)
        for bug in new_bugs:
            bug.screenshot_path = ss_path

    filed_bugs = _file_event_bugs(s, new_bugs)

    if not new_bugs and not console_errs and not network_errs:
        return f"No new console or network events. Total bugs in session: {len(s.bugs)}"

    lines = []
    for err in console_errs:
        lines.append(f"[CONSOLE {err['type'].upper()}] {err['text']}")
    for err in network_errs:
        if err.get("status") is None:
            lines.append(
                f"[REQUEST FAILED: {err.get('failure', 'no response')}] "
                f"{err['method']} {_redact(err['url'])}"
            )
        else:
            lines.append(f"[HTTP {err['status']}] {err['method']} {_redact(err['url'])}")
    if new_bugs:
        merged = len(new_bugs) - len(filed_bugs)
        lines.append(
            f"\nCaptured {len(new_bugs)} browser event(s) as {len(filed_bugs)} new finding(s)."
        )
        if merged:
            lines.append(f"Attached {merged} event(s) to an existing root-cause finding.")
    lines.append(f"Total bugs in session: {len(s.bugs)}")

    return "\n".join(lines)


_SEVERITY_BY_NAME = {s.value: s for s in Severity}


_BUG_TYPE_BY_NAME = {t.value: t for t in BugType}
