from __future__ import annotations

from argus.domain.models import ExplorationResult


def render_junit(r: ExplorationResult) -> str:
    # Each finding is a <testcase> with a <failure> — a CI pipeline treats the
    # suite as failed if Argus found anything, the shift-left "did we ship a
    # bug?" gate. Verdict is surfaced so a consumer can distinguish proven from
    # unverified. Built with ElementTree for correct XML escaping.
    import xml.etree.ElementTree as ET

    findings = [b.to_dict() for b in r.bugs]
    failures = sum(
        1 for f in findings if (f.get("reproduction") or {}).get("reproduced") is not False
    )
    suite = ET.Element(
        "testsuite",
        {
            "name": "argus",
            "tests": str(len(findings)),
            "failures": str(failures),
            "hostname": r.url or "",
            "time": f"{r.duration_seconds or 0:.1f}",
        },
    )
    for f in findings:
        verdict = (f.get("reproduction") or {}).get("reproduced")
        tier = "PROVEN" if verdict is True else ("REFUTED" if verdict is False else "UNVERIFIED")
        tc = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"argus.{f['severity']}",
                "name": f"[{tier}] {f['title']}"[:250],
            },
        )
        body_lines = [
            f"URL: {f['url']}",
            f"Type: {f['type']}",
            f"Severity: {f['severity']}",
            f"Verdict: {tier}",
            "",
            f["description"] or "",
        ]
        if f.get("steps_to_reproduce"):
            body_lines += ["", "Steps:"] + [
                f"  {i + 1}. {s}" for i, s in enumerate(f["steps_to_reproduce"])
            ]
        # A REFUTED finding is not a build failure — record it as skipped, honest.
        if verdict is False:
            ET.SubElement(tc, "skipped", {"message": "symptom not reproduced on clean load"})
        else:
            fail = ET.SubElement(tc, "failure", {"message": f["title"][:200], "type": f["type"]})
            fail.text = "\n".join(body_lines)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")
