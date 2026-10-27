from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

import argus.mcp_server as m
from argus.adapters.reporting.reporter import Reporter
from argus.cleanup import execute_cleanup, plan_cleanup
from argus.cli import cleanup
from argus.domain.models import ExplorationResult
from argus.storage import (
    StorageError,
    atomic_write_text,
    exclusive_file_lock,
    read_journal,
    write_journal,
)


def _result(run_id: str) -> ExplorationResult:
    return ExplorationResult(
        url="https://example.test",
        bugs=[],
        pages_visited=[],
        actions_taken=0,
        duration_seconds=0,
        focus_areas=[],
        run_id=run_id,
    )


def test_atomic_write_replaces_content_without_temp_files(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")

    assert target.read_text() == "second"
    assert list(tmp_path.glob("*.tmp")) == []


def test_storage_lock_times_out_instead_of_racing(tmp_path):
    lock = tmp_path / "journal.lock"
    with exclusive_file_lock(lock):
        with pytest.raises(StorageError, match="timed out"):
            with exclusive_file_lock(lock, timeout_seconds=0.01):
                pass
    with exclusive_file_lock(lock, timeout_seconds=0):
        assert lock.exists()


def test_stale_storage_lock_is_recovered(tmp_path):
    lock = tmp_path / "journal.lock"
    lock.write_text("stale")
    old = time.time() - 120
    os.utime(lock, (old, old))

    with exclusive_file_lock(lock, stale_after_seconds=60):
        assert lock.exists()

    with exclusive_file_lock(lock, timeout_seconds=0):
        assert lock.exists()


def test_journal_reads_legacy_list_and_writes_versioned_document(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text(json.dumps([{"title": "legacy"}]))
    assert read_journal(path) == [{"title": "legacy"}]

    write_journal(path, [{"title": "current"}])
    document = json.loads(path.read_text())
    assert document == {
        "schema_version": 1,
        "entries": [{"title": "current"}],
    }


def test_corrupt_journal_is_diagnosed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ARGUS_OUTPUT_DIR", str(tmp_path))
    m.runtime.settings = replace(m.runtime.settings, output_dir=Path(str(tmp_path)))
    path = m._journal_path("example.test")
    path.parent.mkdir(parents=True)
    path.write_text("{broken")

    assert m._journal_entries("example.test") == []
    assert "storage warning" in capsys.readouterr().err


def test_report_run_id_prevents_cross_run_overwrite(tmp_path):
    first = Reporter().generate(_result("run_one"), str(tmp_path))
    second = Reporter().generate(_result("run_two"), str(tmp_path))

    assert Path(first).name == "report_run_one.html"
    assert Path(second).name == "report_run_two.html"
    assert Path(first).exists() and Path(second).exists()


def test_report_rejects_unsafe_run_id(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        Reporter().generate(_result("../escape"), str(tmp_path))


def test_secondary_report_failure_is_visible_and_does_not_block_siblings(monkeypatch, tmp_path):
    import argus.adapters.reporting.reporter as reporter_module

    real_write = reporter_module.atomic_write_text

    def fail_json(path, text, encoding="utf-8"):
        if path.suffix == ".json":
            raise StorageError("json unavailable")
        real_write(path, text, encoding)

    monkeypatch.setattr(reporter_module, "atomic_write_text", fail_json)
    reporter = Reporter()
    reporter.generate(_result("partial"), str(tmp_path))

    assert reporter.warnings and "json unavailable" in reporter.warnings[0]
    assert (tmp_path / "report_partial.html").exists()
    assert (tmp_path / "report_partial.junit.xml").exists()
    assert (tmp_path / "report_partial.sarif").exists()


def test_cleanup_removes_complete_old_run_and_preserves_state(tmp_path):
    old = Reporter().generate(_result("old"), str(tmp_path))
    new = Reporter().generate(_result("new"), str(tmp_path))
    old_assets = tmp_path / "report-assets" / "report_old"
    old_assets.mkdir(parents=True)
    (old_assets / "shot.webp").write_bytes(b"x" * 20)
    old_screens = tmp_path / "screenshots" / "old"
    old_screens.mkdir(parents=True)
    (old_screens / "shot.png").write_bytes(b"x" * 30)
    state = tmp_path / ".argus" / "journal" / "site.json"
    state.parent.mkdir(parents=True)
    state.write_text("[]")

    for path in [*tmp_path.glob("report_old.*"), *old_assets.rglob("*"), *old_screens.rglob("*")]:
        os.utime(path, (1, 1))
    os.utime(old_assets, (1, 1))
    os.utime(old_screens, (1, 1))

    plan = plan_cleanup(tmp_path, keep_runs=1)
    assert [run.run_id for run in plan.selected] == ["old"]
    execute_cleanup(plan)

    assert not Path(old).exists()
    assert not old_assets.exists()
    assert not old_screens.exists()
    assert Path(new).exists()
    assert state.exists()


def test_cleanup_cli_is_dry_run_by_default(tmp_path):
    old = Reporter().generate(_result("old"), str(tmp_path))
    Reporter().generate(_result("new"), str(tmp_path))

    result = CliRunner().invoke(cleanup, ["--output", str(tmp_path), "--keep-runs", "1"])

    assert result.exit_code == 0
    assert "Dry run only" in result.output
    assert Path(old).exists()
