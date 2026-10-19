from __future__ import annotations

from datetime import datetime
from typing import Dict

from argus.domain.models import ExplorationResult


def render_json(r: ExplorationResult) -> str:
    import json

    findings = [b.to_dict() for b in r.bugs]
    verified = sum(1 for f in findings if f.get("verified"))
    by_sev: Dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    doc = {
        "schema_version": 1,
        "tool": "argus",
        "run_id": r.run_id,
        "url": r.url,
        "generated_at": datetime.now().isoformat(),
        "duration_seconds": r.duration_seconds,
        "review_mode": r.review_mode,
        "tool_calls": r.tool_calls,
        "actions_taken": r.actions_taken,
        "focus_areas": list(r.focus_areas or []),
        "constraints": list(r.constraints or []),
        "coverage": dict(r.coverage or {}),
        "pages_visited": list(r.pages_visited or []),
        "screenshots": [s.to_dict() for s in r.screenshots],
        "observations": [o.to_dict() for o in r.observations],
        "summary": {"total": len(findings), "verified": verified, "by_severity": by_sev},
        "findings": findings,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)
