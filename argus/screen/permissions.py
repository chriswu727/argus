"""macOS screen-mode permission probing.

Argus screen-mode (#27 onwards) needs two macOS privacy grants to do
useful work:

  - Screen Recording: required for `screencapture` to actually capture
    application windows (without it the system silently returns the
    desktop wallpaper or a black image).
  - Accessibility: required to query the AXUIElement tree (the structured
    UI representation we use to resolve descriptions to elements) and
    to synthesise input events.

This module probes both passively. It does not request the prompts —
the user must grant via System Settings, then re-run.

Cross-platform: probes return granted=True with a "n/a" detail on
non-Darwin platforms so #27/#28 can short-circuit cleanly.
"""
from __future__ import annotations

import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class PermissionCheck:
    """The outcome of a single permission probe."""
    name: str          # human-readable name
    granted: bool      # was the probe satisfied?
    detail: str        # short reason / observed result
    settings_url: str  # `x-apple.systempreferences:` deep link to grant


def is_macos() -> bool:
    return platform.system() == "Darwin"


def check_screen_recording() -> PermissionCheck:
    """Probe Screen Recording by capturing a 100x100 patch of the screen.

    Without the grant, modern macOS silently returns a tiny / black /
    desktop-only image. With the grant we get a real RGBA capture above
    a few KB. Empirically a clean granted capture is >1 KB; a denied
    one is typically <500 B.
    """
    if not is_macos():
        return PermissionCheck(
            "Screen Recording", True, "n/a (non-macOS)", "",
        )

    settings_url = (
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_ScreenCapture"
    )

    tmp = Path(tempfile.gettempdir()) / "argus_screen_probe.png"
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    try:
        result = subprocess.run(
            ["screencapture", "-t", "png", "-x", "-R", "0,0,100,100", str(tmp)],
            capture_output=True, timeout=5,
        )
    except FileNotFoundError:
        return PermissionCheck(
            "Screen Recording", False,
            "`screencapture` binary not found",
            settings_url,
        )
    except subprocess.TimeoutExpired:
        return PermissionCheck(
            "Screen Recording", False,
            "screencapture timed out — system may be prompting",
            settings_url,
        )

    if not tmp.exists():
        return PermissionCheck(
            "Screen Recording", False,
            "screencapture produced no output (rc="
            f"{result.returncode})",
            settings_url,
        )

    size = tmp.stat().st_size
    if size < 500:
        return PermissionCheck(
            "Screen Recording", False,
            f"capture produced a {size}-byte image (suspiciously small — "
            "likely permission denied)",
            settings_url,
        )

    return PermissionCheck(
        "Screen Recording", True, f"captured 100x100 patch ({size} bytes)",
        settings_url,
    )


def check_accessibility() -> PermissionCheck:
    """Probe Accessibility via osascript.

    `tell application "System Events" to get name of first process`
    only succeeds if the process running osascript is trusted, which
    in turn requires the parent terminal / IDE to hold the
    Accessibility grant.
    """
    if not is_macos():
        return PermissionCheck(
            "Accessibility", True, "n/a (non-macOS)", "",
        )

    settings_url = (
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Accessibility"
    )

    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process',
            ],
            capture_output=True,
            timeout=5,
            text=True,
        )
    except FileNotFoundError:
        return PermissionCheck(
            "Accessibility", False, "`osascript` not found", settings_url,
        )
    except subprocess.TimeoutExpired:
        return PermissionCheck(
            "Accessibility", False,
            "osascript timed out — system may be prompting",
            settings_url,
        )

    if result.returncode == 0 and result.stdout.strip():
        return PermissionCheck(
            "Accessibility", True,
            f"can query System Events (sees {result.stdout.strip()!r})",
            settings_url,
        )

    err = (result.stderr or "").strip().splitlines()[-1] if result.stderr else ""
    return PermissionCheck(
        "Accessibility", False,
        f"osascript failed: {err[:160] or 'no error message'}",
        settings_url,
    )


def check_all() -> List[PermissionCheck]:
    return [check_screen_recording(), check_accessibility()]


def render_report(checks: List[PermissionCheck]) -> str:
    """Format the probe results for `argus-mcp --doctor`."""
    lines = ["Argus screen-mode permission check"]
    if not is_macos():
        lines.append(
            "Running on a non-macOS host; screen mode is currently "
            "macOS-only. All checks reported as n/a."
        )
        return "\n".join(lines)

    lines.append("")
    all_ok = True
    for c in checks:
        marker = "[ok]" if c.granted else "[MISSING]"
        lines.append(f"  {marker} {c.name}")
        lines.append(f"        {c.detail}")
        if not c.granted:
            all_ok = False
            lines.append(
                "        Grant via: System Settings -> Privacy & Security -> "
                f"{c.name}"
            )
            lines.append(f"        Open directly: {c.settings_url}")
        lines.append("")

    if all_ok:
        lines.append(
            "All checks passed. Argus screen mode is ready to launch."
        )
    else:
        lines.append(
            "Fix the missing grants above, then re-run "
            "`argus-mcp --doctor` to confirm. Screen-mode tools "
            "(observe / click_what / type_into in screen mode) will "
            "refuse to start while any grant is missing."
        )
    return "\n".join(lines)


def gate_screen_mode() -> List[PermissionCheck]:
    """Used by screen-mode tools at startup. Returns the missing checks
    so the caller can refuse with an actionable error message.
    """
    return [c for c in check_all() if not c.granted]


def main() -> int:
    """Entry point for `argus-mcp --doctor` and standalone use."""
    checks = check_all()
    print(render_report(checks))
    return 0 if all(c.granted for c in checks) else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
