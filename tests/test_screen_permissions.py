"""Tests for argus.screen.permissions."""
from __future__ import annotations

from unittest.mock import patch

from argus.screen import permissions


def test_render_report_all_ok_on_macos():
    checks = [
        permissions.PermissionCheck("Screen Recording", True, "captured 100x100 patch (12345 bytes)", "x-apple..."),
        permissions.PermissionCheck("Accessibility", True, "sees Finder", "x-apple..."),
    ]
    with patch.object(permissions, "is_macos", return_value=True):
        out = permissions.render_report(checks)
    assert "All checks passed" in out
    assert "[ok] Screen Recording" in out
    assert "[ok] Accessibility" in out
    assert "MISSING" not in out


def test_render_report_lists_settings_url_on_failure():
    checks = [
        permissions.PermissionCheck(
            "Screen Recording", False,
            "capture produced a 100-byte image",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        ),
        permissions.PermissionCheck("Accessibility", True, "ok", "x-apple..."),
    ]
    with patch.object(permissions, "is_macos", return_value=True):
        out = permissions.render_report(checks)
    assert "[MISSING] Screen Recording" in out
    assert "x-apple.systempreferences" in out
    assert "Privacy_ScreenCapture" in out
    assert "Fix the missing grants above" in out


def test_render_report_non_macos_short_circuits():
    with patch.object(permissions, "is_macos", return_value=False):
        out = permissions.render_report([])
    assert "non-macOS" in out
    assert "n/a" in out


def test_check_all_returns_two_probes():
    checks = permissions.check_all()
    assert len(checks) == 2
    names = {c.name for c in checks}
    assert names == {"Screen Recording", "Accessibility"}


def test_gate_screen_mode_filters_to_missing():
    fake = [
        permissions.PermissionCheck("Screen Recording", True, "ok", ""),
        permissions.PermissionCheck("Accessibility", False, "denied", "url"),
    ]
    with patch.object(permissions, "check_all", return_value=fake):
        missing = permissions.gate_screen_mode()
    assert len(missing) == 1
    assert missing[0].name == "Accessibility"


def test_non_macos_probes_short_circuit_to_granted():
    with patch.object(permissions, "is_macos", return_value=False):
        sr = permissions.check_screen_recording()
        ax = permissions.check_accessibility()
    assert sr.granted is True
    assert ax.granted is True
    assert "n/a" in sr.detail
    assert "n/a" in ax.detail
