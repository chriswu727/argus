from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from argus.domain.models import ExplorationResult, validated_run_id
from argus.measurement import timed
from argus.security import redacted_copy
from argus.storage import StorageError, atomic_write_text, exclusive_file_lock

from .html import render_html
from .json import render_json
from .junit import render_junit
from .sarif import render_sarif


class Reporter:
    """Generates HTML and machine-readable QA reports."""

    _build_sarif = staticmethod(render_sarif)
    _build_json = staticmethod(render_json)
    _build_junit = staticmethod(render_junit)
    _build_html = staticmethod(render_html)

    def __init__(self, exporters=None) -> None:
        self.warnings: List[str] = []
        self.exporters = (
            exporters
            if exporters is not None
            else {
                "json": render_json,
                "junit.xml": render_junit,
                "sarif": render_sarif,
            }
        )
        if any(
            not extension or not all(c.isalnum() or c == "." for c in extension)
            for extension in self.exporters
        ):
            raise ValueError("report extensions must contain only letters, digits and dots")

    @timed("report_export")
    def generate(
        self,
        result: ExplorationResult,
        output_dir: str,
        portable: Optional[bool] = None,
    ) -> str:
        result = redacted_copy(result)
        run_id = validated_run_id(result.run_id)
        lock = Path(output_dir) / ".argus" / "locks" / f"{run_id}.lock"
        with exclusive_file_lock(lock):
            return self._generate(result, output_dir, portable)

    def _generate(
        self,
        result: ExplorationResult,
        output_dir: str,
        portable: Optional[bool] = None,
    ) -> str:
        self.warnings = []
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = validated_run_id(result.run_id)
        path = out / f"report_{ts}.html"
        if portable is None:
            portable = os.environ.get("ARGUS_PORTABLE_REPORT", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        asset_dir = None if portable else out / "report-assets" / f"report_{ts}"
        atomic_write_text(
            path,
            self._build_html(
                result,
                report_dir=out,
                portable=portable,
                asset_dir=asset_dir,
            ),
        )
        # Machine-readable siblings so Argus is consumable, not just readable:
        # JSON for API/programmatic use, JUnit XML for CI (the universal format
        # a pipeline can gate on). Best-effort — a serialization hiccup must not
        # cost the human-readable report that already succeeded.
        siblings = (
            (out / f"report_{ts}.{extension}", build) for extension, build in self.exporters.items()
        )
        for sibling, build in siblings:
            try:
                atomic_write_text(sibling, build(result))
            except (StorageError, ValueError, TypeError) as exc:
                self.warnings.append(f"could not write {sibling.name}: {type(exc).__name__}: {exc}")
        return str(path)
