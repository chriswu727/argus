from __future__ import annotations

from typing import Dict

from argus.domain.models import ExplorationResult, Severity

from .common import (
    _BUGTYPE_LABELS,
)


def render_sarif(r: ExplorationResult) -> str:
    import json

    _LEVEL = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "note"}
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
        verdict = (d.get("reproduction") or {}).get("reproduced")
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
                    "severity": d["severity"],
                },
            }
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
            }
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)
