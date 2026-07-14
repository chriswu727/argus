from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from PIL import Image

from .browser import _redact
from .models import Bug, BugType, ExplorationResult, Screenshot, Severity

_BUGTYPE_LABELS = {
    BugType.CONSOLE_ERROR: "Console Error",
    BugType.NETWORK_ERROR: "Network Error",
    BugType.STATE_VERIFICATION: "State Verification",
    BugType.MISLEADING_SUCCESS: "Misleading Success",
    BugType.COUNT_MISMATCH: "Count Mismatch",
    BugType.TEXT_ANOMALY: "Text Anomaly",
    BugType.UX_ISSUE: "UX Issue",
    BugType.VISUAL_ANOMALY: "Visual Anomaly",
    BugType.CRASH: "Crash",
    BugType.BROKEN_LINK: "Broken Link",
    BugType.FORM_ERROR: "Form Error",
    BugType.BROKEN_IMAGE: "Broken Image",
    BugType.SEO_ISSUE: "SEO Issue",
    BugType.ACCESSIBILITY: "Accessibility",
    BugType.PERFORMANCE: "Performance",
    BugType.MIXED_CONTENT: "Mixed Content",
}


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _repro_badge(receipt: Optional[dict]) -> str:
    """Render the reproduction receipt as a small inline badge on a bug card.

    Empty string when there's no receipt — most bugs are observation-based
    and shouldn't carry a misleading 'unverified' mark.
    """
    if not receipt:
        return ""  # observation-based finding — no machine-checkable symptom
    if not receipt.get("attempted"):
        # A receipt that exists but wasn't attempted is either an
        # auto-captured event bug or a verify clause that was rejected. Render
        # each distinctly so the reader never confuses them with a confirmed
        # finding (or with an observation-based bug, which carries no receipt).
        if receipt.get("auto_captured"):
            return ('<span class="rp" style="background:#6e7781">'
                    'AUTO-CAPTURED EVENT · not independently verified</span>')
        return ('<span class="rp" style="background:#6e7781">'
                'VERIFY NOT RUN · clause rejected</span>')
    reproduced = receipt.get("reproduced")
    is_replay = receipt.get("mode") == "replay"
    if reproduced is True:
        if is_replay:
            label = f"VERIFIED · reproduced by replaying {receipt.get('steps', '?')} steps from a cold start"
        else:
            runs = str(receipt.get("runs", "")).replace("/", " of ")
            label = (f"VERIFIED · reproduced on {runs} clean reloads"
                     if runs.strip() else "VERIFIED · reproduced on a clean reload")
        color = "#1a7f37"
    elif reproduced is False:
        if is_replay:
            color, label = "#b35900", f"NOT REPRODUCED · replayed {receipt.get('steps', '?')} steps, symptom absent"
        elif receipt.get("expect_status") is not None:
            color, label = "#b35900", f"NOT REPRODUCED · expected HTTP {receipt['expect_status']}"
        elif receipt.get("flaky"):
            color, label = "#9a6700", f"INTERMITTENT · {receipt.get('runs', '')} on reload"
        else:
            color, label = "#b35900", "NOT REPRODUCED · absent on clean reload (may be intermittent — re-check)"
    else:  # None — inconclusive
        if is_replay and receipt.get("diverged"):
            color, label = "#6e7781", "INCONCLUSIVE · replay path diverged (a step no longer resolves)"
        elif is_replay:
            color, label = "#6e7781", "INCONCLUSIVE · symptom pre-existed the journey (not caused by these steps)"
        elif receipt.get("reason"):
            color, label = "#6e7781", f"INCONCLUSIVE · {receipt['reason']}"
        elif receipt.get("error"):
            color, label = "#6e7781", "REPRO CHECK ERRORED"
        else:
            color, label = "#6e7781", "INCONCLUSIVE · no verdict"
    return (f'<span class="rp" style="background:{color}">{_esc(label)}</span>')


def _repro_detail(receipt: Optional[dict]) -> str:
    """A body line stating WHAT was independently re-checked, so a reader sees
    the precision moat at work — not just a verdict badge. Only for attempted
    receipts that reached a true/false verdict on a named target."""
    if not receipt or not receipt.get("attempted"):
        return ""
    target = receipt.get("target_text")
    reproduced = receipt.get("reproduced")
    if reproduced is None:
        reason = receipt.get("reason") or receipt.get("error")
        if reason:
            return f'<div class="rd">Independent re-check was inconclusive: {_esc(str(reason))}.</div>'
        return ""
    if not target:
        if receipt.get("expect_status") is not None:
            expected = receipt["expect_status"]
            observed = ", ".join(str(item) for item in receipt.get("observed_statuses", []))
            if reproduced is True:
                body = f"Independently confirmed on fresh loads: HTTP status is <code>{expected}</code>."
            else:
                body = f"Could not independently confirm HTTP <code>{expected}</code>; observed {observed or 'no response'}."
            return f'<div class="rd">{body}</div>'
        return ""
    expect = (receipt.get("expect") or "present").strip().lower()
    # Collapse any literal newlines/whitespace the model baked into the target.
    tgt = " ".join(str(target).split())
    state = "present on the page" if expect == "present" else "absent from the page"
    if reproduced is True:
        # A plain human sentence, not "expected X absent". The card already shows
        # the URL right above, so don't repeat it here.
        body = f'Independently confirmed on a fresh load: <code>{_esc(tgt)}</code> is {state}.'
    else:
        want = "present" if expect == "present" else "absent"
        body = (f'Could not independently confirm on a fresh load: '
                f'<code>{_esc(tgt)}</code> was expected {want}, but the re-check disagreed.')
    return f'<div class="rd">{body}</div>'


def _embed_image(path: str) -> Optional[str]:
    """Read an image file and return a base64 data URI."""
    p = Path(path)
    if not p.exists():
        return None
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    suffix = p.suffix.lower()
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")
    return f"data:{mime};base64,{b64}"


def _prepare_report_image(path: str, report_dir: Path, asset_dir: Path) -> Optional[str]:
    source = Path(path).expanduser()
    if not source.exists():
        return None
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:10]
    stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in source.stem)
    destination = asset_dir / f"{stem[:80]}_{digest}.webp"
    try:
        asset_dir.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image.seek(0)
                image.thumbnail((1600, 6000), Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                image.save(destination, "WEBP", quality=78, method=6)
    except Exception:
        return None
    relative = destination.relative_to(report_dir).as_posix()
    return quote(relative, safe="/")


_SEVERITY_COLORS = {
    Severity.CRITICAL: "#dc2626",
    Severity.HIGH: "#ea580c",
    Severity.MEDIUM: "#ca8a04",
    Severity.LOW: "#2563eb",
    Severity.INFO: "#6b7280",
}

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]
_SEV_IDX = {s: i for i, s in enumerate(_SEVERITY_ORDER)}
_MAX_STEPS = 12


def _format_steps(steps: List[str]) -> str:
    """Render steps-to-reproduce as tight <li>s: collapse consecutive duplicates
    ("click Load More" x2) and trim a long setup preamble to the actionable tail,
    noting what was omitted. A real bug report is the minimal path to the symptom,
    not the agent's entire wandering journey."""
    import re
    # Strip a leading "1." / "2)" the model left on a step — the <ol> numbers it.
    steps = [re.sub(r'^\s*\d+[.)]\s*', '', s).strip() for s in steps if s and s.strip()]
    collapsed: List[list] = []
    for s in steps:
        if collapsed and collapsed[-1][0] == s:
            collapsed[-1][1] += 1
        else:
            collapsed.append([s, 1])
    rendered = [s + (f" (x{n})" if n > 1 else "") for s, n in collapsed]
    items = ""
    if len(rendered) > _MAX_STEPS:
        omitted = len(rendered) - _MAX_STEPS
        rendered = rendered[-_MAX_STEPS:]
        items += f"<li><em>… {omitted} earlier setup/navigation step(s) omitted</em></li>"
    return items + "".join(f"<li>{_esc(s)}</li>" for s in rendered)


def _dedup_description(title: str, description: str) -> str:
    """Drop a description that merely repeats the title. LLMs very often put the
    same sentence in both `title` and `description` (or make the description a
    subset of the title), which renders as the card saying the same thing twice.
    Keep the description only when it genuinely adds information."""
    if not description:
        return ""
    nt = " ".join((title or "").lower().split()).strip(" .!?—-:")
    nd = " ".join(description.lower().split()).strip(" .!?—-:")
    if not nd or nd == nt or nd in nt:
        return ""  # identical to, or a subset of, the title — adds nothing
    return description


def _trust_rank(bug: Bug) -> int:
    """Order findings by how trustworthy they are, so a reader sees the proven
    ones first and the raw unverified noise last (the precision moat, made
    visible in the report): 0 independently VERIFIED, 1 observation-based
    judgment, 2 attempted-but-not-reproduced/inconclusive, 3 auto-captured
    event (console/network noise, never independently confirmed)."""
    r = bug.reproduction_receipt or {}
    if r.get("auto_captured"):
        return 3
    if r.get("reproduced") is True:
        return 0
    if not bug.reproduction_receipt:
        return 1
    return 2


class Reporter:
    """Generates HTML and machine-readable QA reports."""

    def generate(
        self,
        result: ExplorationResult,
        output_dir: str,
        portable: Optional[bool] = None,
    ) -> str:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"report_{ts}.html"
        if portable is None:
            portable = os.environ.get("ARGUS_PORTABLE_REPORT", "").strip().lower() in {
                "1", "true", "yes", "on",
            }
        asset_dir = None if portable else out / "report-assets" / f"report_{ts}"
        path.write_text(
            self._build_html(
                result,
                report_dir=out,
                portable=portable,
                asset_dir=asset_dir,
            ),
            encoding="utf-8",
        )
        # Machine-readable siblings so Argus is consumable, not just readable:
        # JSON for API/programmatic use, JUnit XML for CI (the universal format
        # a pipeline can gate on). Best-effort — a serialization hiccup must not
        # cost the human-readable report that already succeeded.
        try:
            (out / f"report_{ts}.json").write_text(self._build_json(result), encoding="utf-8")
            (out / f"report_{ts}.junit.xml").write_text(self._build_junit(result), encoding="utf-8")
            # SARIF: GitHub code scanning / PR annotations ingest this, so Argus
            # findings surface inline in review — the dev-workflow half of the vision.
            (out / f"report_{ts}.sarif").write_text(self._build_sarif(result), encoding="utf-8")
        except Exception:
            pass
        return str(path)

    def _build_sarif(self, r: ExplorationResult) -> str:
        import json
        _LEVEL = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "note"}
        rules: Dict[str, dict] = {}
        results = []
        for b in r.bugs:
            d = b.to_dict()
            rid = d["type"]
            if rid not in rules:
                rules[rid] = {"id": rid, "name": rid,
                              "shortDescription": {"text": _BUGTYPE_LABELS.get(b.type, rid)}}
            verdict = (d.get("reproduction") or {}).get("reproduced")
            msg = d["title"] + (f" — {d['description']}" if d.get("description") else "")
            results.append({
                "ruleId": rid,
                "level": _LEVEL.get(b.severity, "warning"),
                "message": {"text": msg},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": d["url"] or r.url or "/"}}}],
                # Carry the receipt verdict so a consumer can filter to PROVEN, and
                # so a REFUTED finding can be distinguished from a real one.
                "properties": {"verified": d["verified"], "reproduced": verdict,
                               "severity": d["severity"]},
            })
        doc = {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{
                "tool": {"driver": {
                    "name": "Argus",
                    "informationUri": "https://github.com/chriswu727/argus",
                    "rules": list(rules.values())}},
                "results": results,
            }],
        }
        return json.dumps(doc, indent=2, ensure_ascii=False)

    def _build_json(self, r: ExplorationResult) -> str:
        import json
        findings = [b.to_dict() for b in r.bugs]
        verified = sum(1 for f in findings if f.get("verified"))
        by_sev: Dict[str, int] = {}
        for f in findings:
            by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        doc = {
            "tool": "argus",
            "url": r.url,
            "generated_at": datetime.now().isoformat(),
            "duration_seconds": r.duration_seconds,
            "review_mode": r.review_mode,
            "tool_calls": r.tool_calls,
            "actions_taken": r.actions_taken,
            "focus_areas": list(r.focus_areas or []),
            "pages_visited": list(r.pages_visited or []),
            "screenshots": [s.to_dict() for s in r.screenshots],
            "observations": [o.to_dict() for o in r.observations],
            "summary": {"total": len(findings), "verified": verified, "by_severity": by_sev},
            "findings": findings,
        }
        return json.dumps(doc, indent=2, ensure_ascii=False)

    def _build_junit(self, r: ExplorationResult) -> str:
        # Each finding is a <testcase> with a <failure> — a CI pipeline treats the
        # suite as failed if Argus found anything, the shift-left "did we ship a
        # bug?" gate. Verdict is surfaced so a consumer can distinguish proven from
        # unverified. Built with ElementTree for correct XML escaping.
        import xml.etree.ElementTree as ET
        findings = [b.to_dict() for b in r.bugs]
        failures = sum(
            1 for f in findings
            if (f.get("reproduction") or {}).get("reproduced") is not False
        )
        suite = ET.Element("testsuite", {
            "name": "argus", "tests": str(len(findings)),
            "failures": str(failures), "hostname": r.url or "",
            "time": f"{r.duration_seconds or 0:.1f}",
        })
        for f in findings:
            verdict = (f.get("reproduction") or {}).get("reproduced")
            tier = "PROVEN" if verdict is True else ("REFUTED" if verdict is False else "UNVERIFIED")
            tc = ET.SubElement(suite, "testcase", {
                "classname": f"argus.{f['severity']}",
                "name": f"[{tier}] {f['title']}"[:250],
            })
            body_lines = [f"URL: {f['url']}", f"Type: {f['type']}", f"Severity: {f['severity']}",
                          f"Verdict: {tier}", "", f["description"] or ""]
            if f.get("steps_to_reproduce"):
                body_lines += ["", "Steps:"] + [f"  {i+1}. {s}" for i, s in enumerate(f["steps_to_reproduce"])]
            # A REFUTED finding is not a build failure — record it as skipped, honest.
            if verdict is False:
                ET.SubElement(tc, "skipped", {"message": "symptom not reproduced on clean load"})
            else:
                fail = ET.SubElement(tc, "failure", {
                    "message": f["title"][:200], "type": f["type"]})
                fail.text = "\n".join(body_lines)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode")

    def _build_html(
        self,
        r: ExplorationResult,
        report_dir: Optional[Path] = None,
        portable: bool = True,
        asset_dir: Optional[Path] = None,
    ) -> str:
        image_cache: Dict[str, Optional[str]] = {}

        def image_src(path: str) -> Optional[str]:
            if path not in image_cache:
                if portable or report_dir is None or asset_dir is None:
                    image_cache[path] = _embed_image(path)
                else:
                    image_cache[path] = _prepare_report_image(path, report_dir, asset_dir)
            return image_cache[path]

        by_sev: Dict[Severity, List[Bug]] = {}
        for bug in r.bugs:
            by_sev.setdefault(bug.severity, []).append(bug)

        # summary badges
        summary = ""
        for sev in _SEVERITY_ORDER:
            count = len(by_sev.get(sev, []))
            if count:
                summary += (
                    f'<div class="si"><span class="sv" '
                    f'style="background:{_SEVERITY_COLORS[sev]}">'
                    f"{count}</span> {sev.value}</div>"
                )

        # trust summary — lead with what's proven
        ranks = [_trust_rank(b) for b in r.bugs]
        n_ver, n_obs = ranks.count(0), ranks.count(1)
        n_unproven, n_auto = ranks.count(2), ranks.count(3)
        trust_bits = []
        if n_ver:
            trust_bits.append(f'<b style="color:#1a7f37">{n_ver} verified</b>')
        if n_obs:
            trust_bits.append(f"{n_obs} observation-based")
        if n_unproven:
            trust_bits.append(f"{n_unproven} not reproduced")
        if n_auto:
            trust_bits.append(f"{n_auto} auto-captured (unverified)")
        if trust_bits:
            summary += f'<div class="si" style="opacity:.85">{" · ".join(trust_bits)}</div>'

        # bug cards — trust tier first (verified lead, auto-captured sink), then severity
        cards = ""
        ordered = sorted(r.bugs, key=lambda b: (_trust_rank(b), _SEV_IDX.get(b.severity, 99)))
        for bug in ordered:
            # An auto-captured console/network event fires from the page itself,
            # not from the agent's journey — showing that journey as its "steps
            # to reproduce" is misleading. State honestly how it was observed.
            if (bug.reproduction_receipt or {}).get("auto_captured"):
                steps_block = ('<div class="st"><strong>How it surfaced:</strong> '
                               '<span class="ac">Captured by Argus\'s console/network listener while '
                               'on this page — it fires from the page itself, so it is not tied to a '
                               'specific user journey.</span></div>')
            else:
                steps_block = ('<div class="st"><strong>Steps to reproduce:</strong><ol>'
                               f'{_format_steps(bug.steps_to_reproduce)}</ol></div>')
            console = ""
            if bug.console_logs:
                logs = "\n".join(bug.console_logs)
                # A Console Error card already names the error in its headline; the
                # type badge says "Console Error" too. Don't print the same string
                # a third time in a <pre> when the title already contains it.
                if " ".join(logs.lower().split()) not in " ".join(bug.title.lower().split()):
                    console = f'<div class="cl"><strong>Console:</strong><pre>{_esc(logs)}</pre></div>'
            network = ""
            for nl in bug.network_logs:
                network += (
                    f'<div class="nl"><code>{nl.get("method", "")} '
                    f'{_esc(_redact(nl.get("url", "")))} → {nl.get("status", "")}</code></div>'
                )
            ss = ""
            if bug.screenshot_path:
                src = image_src(bug.screenshot_path)
                if src:
                    ss = f'<img src="{src}" class="ss" alt="Bug screenshot">'
            repro = _repro_badge(bug.reproduction_receipt)
            repro_detail = _repro_detail(bug.reproduction_receipt)

            cards += f"""<div class="bc">
<div class="bh"><span class="sv" style="background:{_SEVERITY_COLORS[bug.severity]}">{bug.severity.value.upper()}</span>
<span class="bt">{_BUGTYPE_LABELS.get(bug.type, bug.type.value)}</span>{repro}</div>
<h3>{_esc(bug.title)}</h3>
{f'<p>{_esc(_dd)}</p>' if (_dd := _dedup_description(bug.title, bug.description)) else ''}
<div class="bu">URL: {_esc(_redact(bug.url))}</div>{repro_detail}
{steps_block}
{console}{network}{ss}</div>"""

        pages = "".join(f"<li>{_esc(p)}</li>" for p in r.pages_visited)
        focus = (
            "".join(f"<li>{_esc(f)}</li>" for f in r.focus_areas)
            if r.focus_areas
            else "<li>General exploration</li>"
        )
        no_bugs = '<div class="nb">No bugs found.</div>' if not r.bugs else ""

        observations_html = ""
        if r.observations:
            observations_html = f'<h2>Review Observations ({len(r.observations)})</h2>'
            for observation in r.observations:
                image = ""
                if observation.screenshot_path:
                    src = image_src(observation.screenshot_path)
                    if src:
                        image = f'<img src="{src}" class="ss" alt="Observation screenshot">'
                observations_html += f"""<div class="oc">
<div class="oh"><span class="ot">{_esc(observation.category.upper())}</span>
<span class="ou">{_esc(_redact(observation.url))}</span></div>
<h3>{_esc(observation.title)}</h3>
<p>{_esc(observation.evidence)}</p>{image}</div>"""

        # screenshots timeline
        screenshots_html = ""
        if r.screenshots:
            screenshots_html = '<h2>Testing Timeline</h2><div class="tl">'
            for ss in r.screenshots:
                src = image_src(ss.path)
                if src:
                    screenshots_html += f"""<div class="tc">
<div class="th"><span class="ts">{_esc(ss.step)}</span>
<span class="tu">{_esc(ss.url)}</span></div>
<img src="{src}" class="ti" alt="{_esc(ss.name)}">
</div>"""
            screenshots_html += "</div>"

        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Argus Report — {_esc(r.url)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;padding:2rem}}
.c{{max-width:900px;margin:0 auto}}
h1{{font-size:1.8rem;color:#f8fafc}}
h2{{font-size:1.3rem;margin:2rem 0 1rem;color:#94a3b8;border-bottom:1px solid #334155;padding-bottom:.5rem}}
h3{{font-size:1.1rem;margin:.5rem 0;color:#f1f5f9}}
.m{{color:#64748b;margin-bottom:2rem}}
.sm{{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}}
.si{{display:flex;align-items:center;gap:.5rem;font-size:.95rem}}
.sv{{color:#fff;padding:2px 10px;border-radius:4px;font-size:.8rem;font-weight:600}}
.sg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin:1rem 0}}
.s{{background:#1e293b;padding:1rem;border-radius:8px}}
.sv2{{font-size:1.5rem;font-weight:700;color:#f8fafc}}
.sl{{color:#64748b;font-size:.85rem}}
.bc{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:1.2rem;margin-bottom:1rem;overflow:hidden;word-break:break-word}}
.oc{{background:#172033;border:1px solid #334155;border-left:3px solid #38bdf8;border-radius:8px;padding:1.2rem;margin-bottom:1rem;overflow:hidden;word-break:break-word}}
.oh{{display:flex;gap:.75rem;align-items:center;margin-bottom:.5rem}}
.ot{{color:#7dd3fc;font-size:.75rem;font-weight:700}}
.ou{{color:#64748b;font-size:.75rem;margin-left:auto;word-break:break-all}}
.bh{{display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem}}
.bt{{color:#64748b;font-size:.85rem}}
.rp{{color:#fff;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600;margin-left:auto}}
.bu{{color:#64748b;font-size:.85rem;margin:.5rem 0}}
.rd{{color:#94a3b8;font-size:.85rem;margin:.4rem 0;padding:.4rem .6rem;background:#0f172a;border-left:2px solid #1a7f37;border-radius:3px}}
.rd code{{color:#e2e8f0}}
.ac{{color:#94a3b8;font-style:italic}}
.st ol{{margin:.5rem 0 .5rem 1.5rem;color:#cbd5e1;word-break:break-word}}
.st li{{margin:.2rem 0}}
.cl pre{{background:#0f172a;padding:.75rem;border-radius:4px;overflow-x:auto;font-size:.85rem;color:#fbbf24;margin-top:.3rem;white-space:pre-wrap;word-break:break-all}}
.nl code{{color:#f87171;font-size:.85rem}}
.ss{{max-width:100%;border-radius:4px;margin-top:.75rem;border:1px solid #334155}}
ul{{margin:.5rem 0 .5rem 1.5rem;color:#cbd5e1}}
li{{margin:.2rem 0}}
.nb{{text-align:center;padding:3rem;color:#22c55e;font-size:1.2rem}}
.tl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:1rem}}
.tc{{background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden}}
.th{{padding:.75rem 1rem;border-bottom:1px solid #334155}}
.ts{{color:#e2e8f0;font-size:.9rem;font-weight:500;display:block}}
.tu{{color:#64748b;font-size:.75rem;display:block;margin-top:.2rem;word-break:break-all}}
.ti{{width:100%;display:block}}
</style></head>
<body><div class="c">
<h1>Argus QA Report</h1>
<div class="m">{_esc(r.url)} — {_esc(r.review_mode)} review — {r.timestamp.strftime("%Y-%m-%d %H:%M:%S")} — {r.duration_seconds:.1f}s</div>
<div class="sg">
<div class="s"><div class="sv2">{len(r.bugs)}</div><div class="sl">Bugs Found</div></div>
<div class="s"><div class="sv2">{len(r.observations)}</div><div class="sl">Observations</div></div>
<div class="s"><div class="sv2">{r.tool_calls}</div><div class="sl">Tool Calls</div></div>
<div class="s"><div class="sv2">{r.actions_taken}</div><div class="sl">Recorded Steps</div></div>
<div class="s"><div class="sv2">{len(r.pages_visited)}</div><div class="sl">Pages Visited</div></div>
<div class="s"><div class="sv2">{len(r.screenshots)}</div><div class="sl">Screenshots</div></div>
</div>
<h2>Focus Areas</h2><ul>{focus}</ul>
<h2>Bugs ({len(r.bugs)})</h2><div class="sm">{summary}</div>
{cards}{no_bugs}
{observations_html}
{screenshots_html}
<h2>Pages Visited</h2><ul>{pages}</ul>
</div></body></html>"""
