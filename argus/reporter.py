from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .adapters.reporting.common import _BUGTYPE_LABELS as _BUGTYPE_LABELS
from .adapters.reporting.common import _MAX_STEPS as _MAX_STEPS
from .adapters.reporting.common import _SEV_IDX as _SEV_IDX
from .adapters.reporting.common import _SEVERITY_COLORS as _SEVERITY_COLORS
from .adapters.reporting.common import _SEVERITY_ORDER as _SEVERITY_ORDER
from .adapters.reporting.common import _dedup_description as _dedup_description
from .adapters.reporting.common import _embed_image as _embed_image
from .adapters.reporting.common import _esc as _esc
from .adapters.reporting.common import _format_steps as _format_steps
from .adapters.reporting.common import _prepare_report_image as _prepare_report_image
from .adapters.reporting.common import _repro_badge as _repro_badge
from .adapters.reporting.common import _repro_detail as _repro_detail
from .adapters.reporting.common import _trust_rank as _trust_rank
from .adapters.reporting.html import render_html
from .adapters.reporting.json import render_json
from .adapters.reporting.junit import render_junit
from .adapters.reporting.sarif import render_sarif
from .models import ExplorationResult, validated_run_id
from .storage import StorageError, atomic_write_text, exclusive_file_lock


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

    def generate(
        self,
        result: ExplorationResult,
        output_dir: str,
        portable: Optional[bool] = None,
    ) -> str:
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
