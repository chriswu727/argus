from __future__ import annotations

import sys

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


def test_missing_browser_reports_install_command(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    (check,) = doctor.check_browser()
    assert not check.ok and "not installed for Playwright" in check.detail
    assert check.fix == f"{sys.executable} -m playwright install chromium"


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


async def test_start_session_launch_failure_names_the_install_command(
    isolated_runtime, monkeypatch, tmp_path
):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    result = await isolated_runtime.execute("start_session", start_session, "https://example.test/")
    assert result.error is not None
    text = str(result.value)
    assert f"{sys.executable} -m playwright install chromium" in text
    assert "argus-mcp --doctor" in text
