from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from argus.storage import (
    StorageError,
    atomic_write_json,
    atomic_write_text,
    read_json,
)

from .evidence import _output_dir, _record_coverage_action
from .session import _require_session
from .text import _marker_visible


def _capsule_dir() -> Path:
    return Path(_output_dir()) / ".argus" / "capsules"


def _capsule_path(origin: str, name: str) -> Path:
    safe_origin = re.sub(r"[^A-Za-z0-9._-]", "_", origin or "default")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "default")
    # Capsules hold raw auth cookies. They live under .argus/ (gitignored), and
    # capsule_save also drops a defensive `.gitignore: *` so they can never be
    # committed even if ARGUS_OUTPUT_DIR points outside the default tree.
    return _capsule_dir() / safe_origin / f"{safe_name}.json"


async def capsule_save(name: str, liveness_marker: str = "") -> str:
    """Snapshot the current logged-in / seeded state as a named, restorable capsule.

    Captures cookies + localStorage + sessionStorage + the current URL. Mint the
    state through the REAL UI first (sign up, create data) and THEN save — that
    keeps it honest (if onboarding is broken, you felt it). Pass a
    `liveness_marker`: text visible ONLY while the capsule is still valid (e.g.
    the logged-in user's name) so a later restore can tell live from stale.

    Args:
        name: Capsule name to save under (per-origin).
        liveness_marker: Text that proves the restored state is still valid.
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return "capsule_save: this tool is web-mode only."
    capsule = await s.browser.capsule_capture()
    capsule["schema_version"] = 1
    capsule["liveness_marker"] = liveness_marker.strip()
    origin = urlparse(capsule.get("url") or "").netloc or "default"
    path = _capsule_path(origin, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Defensive: capsules carry live auth cookies — never let them be
        # committed, even under a non-default ARGUS_OUTPUT_DIR.
        gi = _capsule_dir() / ".gitignore"
        if not gi.exists():
            atomic_write_text(gi, "*\n")
        atomic_write_json(path, capsule)
    except Exception as exc:
        return f"capsule_save: could not write capsule — {type(exc).__name__}: {str(exc)[:120]}"
    s.steps.append(f"capsule_save({name!r})")
    _record_coverage_action(s, "capsule_save", name)
    n_c = len(capsule.get("cookies") or [])
    n_l = len(capsule.get("local") or {})
    n_s = len(capsule.get("session") or {})
    tail = (
        f" Liveness marker: {liveness_marker.strip()!r}."
        if liveness_marker.strip()
        else " No liveness marker set — restore can't verify validity (recommend setting one)."
    )
    return (
        f"Saved capsule {name!r} for {origin} "
        f"({n_c} cookies, {n_l} localStorage, {n_s} sessionStorage).{tail}"
    )


async def capsule_restore(name: str) -> str:
    """Restore a saved capsule onto this session and verify it is still live.

    Sets the cookies + storage, navigates to the captured URL, then checks the
    saved liveness marker. Returns whether the restored state is LIVE or STALE.
    A STALE capsule (the server session expired) cannot be trusted — any bug you
    record afterwards is flagged unreliable until you re-mint the state.

    Args:
        name: Capsule name to restore (looked up for the current origin).
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return "capsule_restore: this tool is web-mode only."
    # Look up by the current origin first, but fall back to scanning every saved
    # origin for this name — so restore works from about:blank or after the app
    # moved to a different port/host alias (the whole point is to skip re-login).
    origin = urlparse(s.browser.current_url).netloc or "default"
    path = _capsule_path(origin, name)
    if not path.exists():
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "default")
        matches = list((Path(_output_dir()) / ".argus" / "capsules").glob(f"*/{safe_name}.json"))
        if len(matches) == 1:
            path = matches[0]
        elif len(matches) > 1:
            origins = ", ".join(p.parent.name for p in matches)
            return (
                f"capsule_restore: {name!r} exists for multiple origins ({origins}); "
                f"navigate to the target origin first to disambiguate."
            )
        else:
            return (
                f"capsule_restore: no capsule named {name!r}. "
                f"capsule_save({name!r}, liveness_marker=...) it first."
            )
    try:
        capsule = read_json(path)
        if not isinstance(capsule, dict):
            raise StorageError(f"invalid capsule structure in {path}")
        if capsule.get("schema_version", 1) != 1:
            raise StorageError("unsupported capsule schema; re-save the capsule")
        applied = await s.browser.capsule_apply(capsule)
    except Exception as exc:
        return f"capsule_restore: failed to apply — {type(exc).__name__}: {str(exc)[:120]}"
    state = await s.browser.get_state()
    s._last_elements = state.elements
    s.steps.append(f"capsule_restore({name!r})")
    _record_coverage_action(s, "capsule_restore", name)

    warn = ""
    if not applied.get("origin_ok", True):
        warn = (
            " WARNING: navigating to the capsule URL redirected cross-origin "
            "(likely an expired session bouncing to login) — storage was NOT applied."
        )
    elif applied.get("cookies_expected", 0) and applied.get("cookies", 0) == 0:
        warn = " WARNING: zero cookies were applied (injection failed) — you are likely NOT authenticated."

    marker = (capsule.get("liveness_marker") or "").strip()
    if not marker:
        s._capsule_marker = None
        return (
            f"Restored capsule {name!r} "
            f"({applied.get('cookies', 0)} cookies, {applied.get('local', 0)} localStorage, "
            f"{applied.get('session', 0)} sessionStorage), but no liveness marker was saved — "
            f"validity is unverified; observe() to confirm.{warn}"
        )
    s._capsule_marker = marker
    if not _marker_visible(marker, state):
        # SPAs render identity after networkidle (a /me fetch + hydration) —
        # give the marker a beat to appear before declaring the session dead.
        await s.browser.wait_for_text(marker, timeout_s=5)
        state = await s.browser.get_state()
        s._last_elements = state.elements
    if _marker_visible(marker, state):
        return (
            f"Restored capsule {name!r} — appears LIVE (marker {marker!r} is visible; "
            f"confirm it's genuinely auth-gated).{warn}"
        )
    return (
        f"Restored capsule {name!r} — STALE: liveness marker {marker!r} is not visible. "
        f"The server session has likely expired; re-mint the state through the UI. "
        f"Bugs recorded while it stays absent are flagged unreliable.{warn}"
    )
