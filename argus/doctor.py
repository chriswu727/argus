"""Environment checks for `argus-mcp --doctor`.

Web checks run everywhere: the Chromium build that the installed Playwright
expects, a real headless launch, and the report directory. Screen-mode
permission probes run only on macOS. Fix commands name this interpreter so
they install into the same environment that runs Argus.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def install_command() -> str:
    return f"{sys.executable} -m playwright install chromium"


def system_deps_command() -> str:
    return f"{sys.executable} -m playwright install-deps chromium"


def browser_missing(exc: BaseException) -> bool:
    return "executable doesn't exist" in str(exc).lower()


def auto_install_enabled() -> bool:
    value = os.environ.get("ARGUS_AUTO_INSTALL_BROWSER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


_install: asyncio.subprocess.Process | None = None


async def install_chromium(headless: bool, wait_seconds: float) -> str:
    """Download the Chromium this Playwright expects.

    Returns "installed", "failed", or "downloading" when it is still running
    after `wait_seconds`. The download is never cancelled: it keeps going in the
    background and the next call waits on the same process, so a slow connection
    costs a retry, not a restart. Its output is discarded because the server's
    stdout is the MCP channel.
    """
    global _install
    deadline = asyncio.get_running_loop().time() + wait_seconds
    # A headless session only needs the smaller headless shell. Playwright
    # releases without that option reject it, so fall back to the full browser.
    for only_shell in [True, False] if headless else [False]:
        if _install is None or _install.returncode is not None:
            command = [sys.executable, "-m", "playwright", "install", "chromium"]
            if only_shell:
                command.append("--only-shell")
            _install = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        process = _install
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), remaining)
        except asyncio.TimeoutError:
            return "downloading"
        _install = None
        if process.returncode == 0:
            return "installed"
    return "failed"


def launch_failure_hint(exc: BaseException) -> str:
    """Name the fix for a Chromium launch failure in this environment."""
    message = str(exc).lower()
    if "install-deps" in message or "missing dependencies" in message:
        return (
            "Chromium is missing system libraries. Run "
            f"`{system_deps_command()}` (needs administrator rights), then try again."
        )
    return (
        f"Install the matching Chromium with `{install_command()}`, then try again. "
        "`argus-mcp --doctor` checks the whole environment."
    )


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}"[:200]


async def _chromium_checks(timeout: float) -> list[Check]:
    from playwright.async_api import async_playwright

    try:
        playwright_version = version("playwright")
    except PackageNotFoundError:
        playwright_version = "unknown"
    async with async_playwright() as playwright:
        # executable_path is the full browser; headless sessions may use the
        # separate headless shell (`--only-shell`), so the launch decides.
        executable = Path(playwright.chromium.executable_path)
        try:
            browser = await asyncio.wait_for(playwright.chromium.launch(headless=True), timeout)
            await browser.close()
        except Exception as exc:
            if not executable.exists():
                return [
                    Check(
                        "Chromium browser",
                        False,
                        f"not installed for Playwright {playwright_version} (expected {executable})",
                        install_command(),
                    )
                ]
            return [Check("Chromium launch", False, _first_line(exc), launch_failure_hint(exc))]
        launched = Check(
            "Chromium launch", True, f"headless launch succeeded (Playwright {playwright_version})"
        )
        if executable.exists():
            return [launched]
        return [
            launched,
            Check(
                "Headed Chromium",
                True,
                "not installed; needed only for start_session(headless=False) "
                f"(`{install_command()}`)",
            ),
        ]


def check_browser(timeout: float = 60.0) -> list[Check]:
    try:
        return asyncio.run(_chromium_checks(timeout))
    except Exception as exc:
        return [Check("Chromium browser", False, _first_line(exc), install_command())]


def check_output_dir() -> Check:
    from argus.settings import Settings

    try:
        directory = Settings.from_env().output_dir
    except ValueError as exc:
        return Check("Report directory", False, f"invalid configuration: {exc}")
    existing = directory
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    # os.access() always says yes to root, so probe with a real file.
    try:
        with tempfile.NamedTemporaryFile(dir=existing, prefix=".argus-doctor-"):
            writable = True
    except OSError:
        writable = False
    detail = str(directory) if directory.exists() else f"{directory} (created on first report)"
    if writable:
        return Check("Report directory", True, detail)
    return Check(
        "Report directory",
        False,
        f"{detail}; {existing} is not writable",
        "Set ARGUS_OUTPUT_DIR to a writable directory.",
    )


def render(checks: list[Check]) -> list[str]:
    lines = []
    for check in checks:
        lines.append(f"  [{'ok' if check.ok else 'MISSING'}] {check.name}")
        lines.append(f"        {check.detail}")
        if check.fix and not check.ok:
            lines.append(f"        Fix: {check.fix}")
    return lines


def main() -> int:
    from argus.screen.permissions import check_all, is_macos, render_report

    web = [*check_browser(), check_output_dir()]
    ok = all(check.ok for check in web)
    lines = ["Argus environment check", "", "Web testing:", *render(web), ""]
    if is_macos():
        screen = check_all()
        ok = ok and all(check.granted for check in screen)
        lines.append(render_report(screen))
    else:
        lines.append("Screen mode: not available on this platform (macOS only).")
    print("\n".join(lines))
    return 0 if ok else 1
