from __future__ import annotations

import sys
from pathlib import Path

import pytest

import argus.doctor as doctor
from argus.application.lifecycle import start_session


def test_launch_hints_name_the_fix_for_this_interpreter():
    missing = doctor.launch_failure_hint(RuntimeError("Executable doesn't exist at /x/chrome"))
    assert f"{sys.executable} -m playwright install chromium" in missing
    assert "--doctor" in missing
    libraries = doctor.launch_failure_hint(
        RuntimeError("Host system is missing dependencies to run browsers")
    )
    assert f"{sys.executable} -m playwright install-deps chromium" in libraries


def test_output_dir_check_probes_with_a_real_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path / "reports"))
    assert doctor.check_output_dir().ok
    assert not list(tmp_path.iterdir())
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(blocker / "reports"))
    check = doctor.check_output_dir()
    assert not check.ok and "ARGUS_OUTPUT_DIR" in check.fix


def test_missing_browser_is_not_a_failure_when_the_first_session_downloads_it(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.delenv("ARGUS_AUTO_INSTALL_BROWSER", raising=False)
    monkeypatch.setattr("argus.screen.permissions.is_macos", lambda: False)
    (check,) = doctor.check_browser()
    assert check.ok and check.on_first_use
    assert "first start_session downloads it" in check.detail
    assert doctor.main() == 0
    output = capsys.readouterr().out
    assert "[first use] Chromium browser" in output and "MISSING" not in output
    assert "Web testing is ready; the browser downloads on the first session." in output


def test_missing_browser_reports_install_command(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setenv("ARGUS_AUTO_INSTALL_BROWSER", "0")
    (check,) = doctor.check_browser()
    assert not check.ok and "not installed for Playwright" in check.detail
    assert check.fix == f"{sys.executable} -m playwright install chromium"


def test_headless_shell_only_installs_pass(monkeypatch, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = next(
            parent
            for parent in Path(playwright.chromium.executable_path).parents
            if parent.name.startswith("chromium-")
        )
    browsers = browser.parent
    shell = browsers / browser.name.replace("chromium-", "chromium_headless_shell-", 1)
    if not shell.is_dir():
        pytest.skip("matching Chromium headless shell is not installed")
    for source in [shell, *browsers.glob("ffmpeg-*")]:
        try:
            (tmp_path / source.name).symlink_to(source)
        except OSError as exc:  # Windows without Developer Mode or admin rights
            pytest.skip(f"cannot create a symlink here: {exc.strerror}")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    checks = doctor.check_browser()
    assert all(check.ok for check in checks)
    assert [check.name for check in checks] == ["Chromium launch", "Headed Chromium"]
    assert "headless=False" in checks[1].detail


def test_doctor_fails_on_missing_web_prerequisites_without_screen_noise(monkeypatch, capsys):
    monkeypatch.setattr("argus.screen.permissions.is_macos", lambda: False)
    monkeypatch.setattr(
        doctor, "check_browser", lambda: [doctor.Check("Chromium", False, "x", "y")]
    )
    monkeypatch.setattr(doctor, "check_output_dir", lambda: doctor.Check("Reports", True, "ok"))
    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "[MISSING] Chromium" in output and "Fix: y" in output
    assert "Screen mode: not available on this platform" in output


@pytest.mark.parametrize("ok", [True, False])
def test_doctor_exit_code_follows_web_checks(monkeypatch, capsys, ok):
    monkeypatch.setattr("argus.screen.permissions.is_macos", lambda: False)
    monkeypatch.setattr(doctor, "check_browser", lambda: [doctor.Check("Chromium", ok, "x")])
    monkeypatch.setattr(doctor, "check_output_dir", lambda: doctor.Check("Reports", True, "ok"))
    assert doctor.main() == (0 if ok else 1)


def _screen_grants(monkeypatch, granted):
    from argus.screen.permissions import PermissionCheck

    monkeypatch.setattr("argus.screen.permissions.is_macos", lambda: True)
    monkeypatch.setattr(
        "argus.screen.permissions.check_all",
        lambda: [PermissionCheck("Screen Recording", granted, "probe", "x-apple:settings")],
    )
    monkeypatch.setattr(doctor, "check_browser", lambda: [doctor.Check("Chromium", True, "x")])
    monkeypatch.setattr(doctor, "check_output_dir", lambda: doctor.Check("Reports", True, "ok"))


@pytest.mark.parametrize("profile", ["web", "advanced"])
def test_missing_screen_grants_do_not_fail_a_web_profile(monkeypatch, capsys, profile):
    _screen_grants(monkeypatch, False)
    assert doctor.main(profile) == 0
    output = capsys.readouterr().out
    assert "Web testing is ready." in output
    assert "[MISSING] Screen Recording" in output
    assert "this check does not fail on them" in output


@pytest.mark.parametrize("profile", ["screen", "full"])
def test_screen_profiles_need_the_screen_grants(monkeypatch, capsys, profile):
    _screen_grants(monkeypatch, False)
    assert doctor.main(profile) == 1
    assert "does not fail on them" not in capsys.readouterr().out
    _screen_grants(monkeypatch, True)
    assert doctor.main(profile) == 0


async def test_start_session_launch_failure_names_the_install_command(
    isolated_runtime, monkeypatch, tmp_path
):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    monkeypatch.setenv("ARGUS_AUTO_INSTALL_BROWSER", "0")
    result = await isolated_runtime.execute("start_session", start_session, "https://example.test/")
    assert result.error is not None
    text = str(result.value)
    assert f"{sys.executable} -m playwright install chromium" in text
    assert "argus-mcp --doctor" in text
