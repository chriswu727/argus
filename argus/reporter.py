from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
            label = f"VERIFIED · reproduced {receipt.get('runs', '')} from clean load"
        color = "#1a7f37"
    elif reproduced is False:
        if is_replay:
            color, label = "#b35900", f"NOT REPRODUCED · replayed {receipt.get('steps', '?')} steps, symptom absent"
        elif receipt.get("flaky"):
            color, label = "#9a6700", f"INTERMITTENT · {receipt.get('runs', '')} on reload"
        else:
            color, label = "#b35900", "NOT REPRODUCED · absent on clean reload (may be intermittent — re-check)"
    else:  # None — inconclusive
        if is_replay and receipt.get("diverged"):
            color, label = "#6e7781", "INCONCLUSIVE · replay path diverged (a step no longer resolves)"
        elif is_replay:
            color, label = "#6e7781", "INCONCLUSIVE · symptom pre-existed the journey (not caused by these steps)"
        else:
            color, label = "#6e7781", "repro check errored"
    return (f'<span class="rp" style="background:{color}">{_esc(label)}</span>')


def _embed_image(path: str) -> Optional[str]:
    """Read an image file and return a base64 data URI."""
    p = Path(path)
    if not p.exists():
        return None
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    suffix = p.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64,{b64}"


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
    """Generates a self-contained HTML error report with embedded screenshots."""

    def generate(self, result: ExplorationResult, output_dir: str) -> str:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"report_{ts}.html"
        path.write_text(self._build_html(result), encoding="utf-8")
        return str(path)

    def _build_html(self, r: ExplorationResult) -> str:
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
            steps = _format_steps(bug.steps_to_reproduce)
            console = ""
            if bug.console_logs:
                logs = "\n".join(bug.console_logs)
                console = f'<div class="cl"><strong>Console:</strong><pre>{_esc(logs)}</pre></div>'
            network = ""
            for nl in bug.network_logs:
                network += (
                    f'<div class="nl"><code>{nl.get("method", "")} '
                    f'{_esc(_redact(nl.get("url", "")))} → {nl.get("status", "")}</code></div>'
                )
            ss = ""
            if bug.screenshot_path:
                data_uri = _embed_image(bug.screenshot_path)
                if data_uri:
                    ss = f'<img src="{data_uri}" class="ss" alt="Bug screenshot">'
            repro = _repro_badge(bug.reproduction_receipt)

            cards += f"""<div class="bc">
<div class="bh"><span class="sv" style="background:{_SEVERITY_COLORS[bug.severity]}">{bug.severity.value.upper()}</span>
<span class="bt">{_BUGTYPE_LABELS.get(bug.type, bug.type.value)}</span>{repro}</div>
<h3>{_esc(bug.title)}</h3>
<p>{_esc(bug.description)}</p>
<div class="bu">URL: {_esc(_redact(bug.url))}</div>
<div class="st"><strong>Steps to reproduce:</strong><ol>{steps}</ol></div>
{console}{network}{ss}</div>"""

        pages = "".join(f"<li>{_esc(p)}</li>" for p in r.pages_visited)
        focus = (
            "".join(f"<li>{_esc(f)}</li>" for f in r.focus_areas)
            if r.focus_areas
            else "<li>General exploration</li>"
        )
        no_bugs = '<div class="nb">No bugs found.</div>' if not r.bugs else ""

        # screenshots timeline
        screenshots_html = ""
        if r.screenshots:
            screenshots_html = '<h2>Testing Timeline</h2><div class="tl">'
            for ss in r.screenshots:
                data_uri = _embed_image(ss.path)
                if data_uri:
                    screenshots_html += f"""<div class="tc">
<div class="th"><span class="ts">{_esc(ss.step)}</span>
<span class="tu">{_esc(ss.url)}</span></div>
<img src="{data_uri}" class="ti" alt="{_esc(ss.name)}">
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
.bh{{display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem}}
.bt{{color:#64748b;font-size:.85rem}}
.rp{{color:#fff;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:600;margin-left:auto}}
.bu{{color:#64748b;font-size:.85rem;margin:.5rem 0}}
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
<div class="m">{_esc(r.url)} — {r.timestamp.strftime("%Y-%m-%d %H:%M:%S")} — {r.duration_seconds:.1f}s</div>
<div class="sg">
<div class="s"><div class="sv2">{len(r.bugs)}</div><div class="sl">Bugs Found</div></div>
<div class="s"><div class="sv2">{r.actions_taken}</div><div class="sl">Actions Taken</div></div>
<div class="s"><div class="sv2">{len(r.pages_visited)}</div><div class="sl">Pages Visited</div></div>
<div class="s"><div class="sv2">{len(r.screenshots)}</div><div class="sl">Screenshots</div></div>
</div>
<h2>Focus Areas</h2><ul>{focus}</ul>
<h2>Bugs ({len(r.bugs)})</h2><div class="sm">{summary}</div>
{cards}{no_bugs}
{screenshots_html}
<h2>Pages Visited</h2><ul>{pages}</ul>
</div></body></html>"""
