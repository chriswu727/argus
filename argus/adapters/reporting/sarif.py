from __future__ import annotations

from typing import Dict

from argus.domain.models import ExplorationResult, Severity
from argus.domain.verification import receipt_outcome

from .common import (
    _BUGTYPE_LABELS,
)
from .privacy import safe_report


@safe_report
def render_sarif(r: ExplorationResult) -> str:
    import json

    _LEVEL = {
        Severity.CRITICAL: "error",
        Severity.HIGH: "error",
        Severity.MEDIUM: "warning",
        Severity.LOW: "note",
        Severity.INFO: "note",
    }
    rules: Dict[str, dict] = {}
    results = []
    for b in r.bugs:
        d = b.to_dict()
        rid = d["type"]
        if rid not in rules:
            rules[rid] = {
                "id": rid,
                "name": rid,
                "shortDescription": {"text": _BUGTYPE_LABELS.get(b.type, rid)},
            }
        verdict = receipt_outcome(d.get("reproduction"))
        msg = d["title"] + (f" — {d['description']}" if d.get("description") else "")
        results.append(
            {
                "ruleId": rid,
                "level": _LEVEL.get(b.severity, "warning"),
                "message": {"text": msg},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": d["url"] or r.url or "/"}}}
                ],
                # Carry the receipt verdict so a consumer can filter to PROVEN, and
                # so a REFUTED finding can be distinguished from a real one.
                "properties": {
                    "verified": d["verified"],
                    "reproduced": verdict,
                    "intermittent": bool((d.get("reproduction") or {}).get("flaky")),
                    "severity": d["severity"],
                },
            }
        )
    # Rechecks of journaled findings, in SARIF's own terms: still present
    # fails, no longer reproducing passes, and an inconclusive check is "open"
    # (undetermined), never a pass. SARIF requires level "none" when kind is
    # not "fail".
    _KIND = {"STILL-PRESENT": "fail", "NO-LONGER-REPRODUCES": "pass", "INCONCLUSIVE": "open"}
    _LEVEL_BY_NAME = {severity.value: level for severity, level in _LEVEL.items()}
    regression = r.regression or {}
    for f in regression.get("findings", []):
        rules.setdefault(
            "regression-recheck",
            {
                "id": "regression-recheck",
                "name": "regression-recheck",
                "shortDescription": {"text": "Fresh-load recheck of a journaled finding"},
            },
        )
        kind = _KIND[f["status"]]
        level = _LEVEL_BY_NAME.get(f["severity"], "error") if kind == "fail" else "none"
        reason = f" — {f['reason']}" if f.get("reason") else ""
        results.append(
            {
                "ruleId": "regression-recheck",
                "kind": kind,
                "level": level,
                "message": {"text": f"[{f['status']}] {f['title']}{reason}"},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": f.get("path") or "/"}}}
                ],
                "properties": {
                    "status": f["status"],
                    "history": f["history"],
                    "runs": f.get("runs"),
                    "severity": f["severity"],
                },
            }
        )
    run_properties = (
        {
            "regression": {
                key: regression[key] for key in ("checks", "counts", "reason") if key in regression
            }
        }
        if regression
        else {}
    )
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Argus",
                        "informationUri": "https://github.com/chriswu727/argus",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
                **({"properties": run_properties} if run_properties else {}),
            }
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)
