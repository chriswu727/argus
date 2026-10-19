from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .storage import StorageError, exclusive_file_lock

_REPORT_FILE = re.compile(r"^report_(.+)\.(?:html|json|junit\.xml|sarif)$")


@dataclass(frozen=True)
class ReportRun:
    run_id: str
    paths: tuple[Path, ...]
    modified_at: float
    size_bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    output_dir: Path
    runs: tuple[ReportRun, ...]
    selected: tuple[ReportRun, ...]
    total_bytes: int
    selected_bytes: int
    legacy_screenshot_files: int
    legacy_screenshot_bytes: int


def _path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_file() or child.is_symlink():
                try:
                    total += child.lstat().st_size
                except OSError:
                    pass
    return total


def discover_report_runs(output_dir: str | Path) -> list[ReportRun]:
    root = Path(output_dir).expanduser().resolve()
    report_paths: dict[str, list[Path]] = {}
    if root.exists():
        for path in root.iterdir():
            match = _REPORT_FILE.match(path.name)
            if match:
                report_paths.setdefault(match.group(1), []).append(path)

    runs: list[ReportRun] = []
    for run_id, report_files in report_paths.items():
        paths = sorted(report_files)
        for directory in (
            root / "report-assets" / f"report_{run_id}",
            root / "screenshots" / run_id,
        ):
            if directory.exists() or directory.is_symlink():
                paths.append(directory)
        if not paths:
            continue
        mtimes = []
        for path in paths:
            try:
                mtimes.append(path.lstat().st_mtime)
            except OSError:
                pass
        runs.append(
            ReportRun(
                run_id=run_id,
                paths=tuple(paths),
                modified_at=max(mtimes, default=0.0),
                size_bytes=sum(_path_size(path) for path in paths),
            )
        )
    return sorted(runs, key=lambda run: (run.modified_at, run.run_id), reverse=True)


def plan_cleanup(
    output_dir: str | Path,
    keep_runs: int = 20,
    older_than_days: int = 0,
    max_bytes: int = 0,
    now: float | None = None,
) -> CleanupPlan:
    if keep_runs < 0 or older_than_days < 0 or max_bytes < 0:
        raise ValueError("cleanup limits cannot be negative")
    runs = discover_report_runs(output_dir)
    current_time = time.time() if now is None else now
    selected: dict[str, ReportRun] = {}

    for index, run in enumerate(runs):
        if index < keep_runs:
            continue
        age_days = (current_time - run.modified_at) / 86_400
        if older_than_days == 0 or age_days >= older_than_days:
            selected[run.run_id] = run

    if max_bytes:
        remaining = sum(run.size_bytes for run in runs if run.run_id not in selected)
        for run in reversed(runs[keep_runs:]):
            if remaining <= max_bytes:
                break
            if run.run_id not in selected:
                selected[run.run_id] = run
                remaining -= run.size_bytes

    chosen = tuple(run for run in runs if run.run_id in selected)
    legacy = []
    screenshot_root = Path(output_dir).expanduser().resolve() / "screenshots"
    if screenshot_root.is_dir():
        legacy = [path for path in screenshot_root.iterdir() if path.is_file()]
    return CleanupPlan(
        output_dir=Path(output_dir).expanduser().resolve(),
        runs=tuple(runs),
        selected=chosen,
        total_bytes=sum(run.size_bytes for run in runs),
        selected_bytes=sum(run.size_bytes for run in chosen),
        legacy_screenshot_files=len(legacy),
        legacy_screenshot_bytes=sum(_path_size(path) for path in legacy),
    )


def execute_cleanup(plan: CleanupPlan) -> tuple[int, int]:
    removed_paths = 0
    removed_bytes = 0
    for run in plan.selected:
        lock = plan.output_dir / ".argus" / "locks" / f"{run.run_id}.lock"
        try:
            with exclusive_file_lock(lock, timeout_seconds=0):
                for path in run.paths:
                    try:
                        if path.is_symlink() or path.is_file():
                            path.unlink()
                        elif path.is_dir():
                            shutil.rmtree(path)
                        else:
                            continue
                        removed_paths += 1
                    except FileNotFoundError:
                        continue
                removed_bytes += run.size_bytes
        except StorageError:
            continue
    return removed_paths, removed_bytes
