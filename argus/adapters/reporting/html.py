from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from argus.domain.models import Bug, ExplorationResult, Severity
from argus.security import _redact

from .common import (
    _BUGTYPE_LABELS,
    _SEV_IDX,
    _SEVERITY_COLORS,
    _SEVERITY_ORDER,
    _dedup_description,
    _embed_image,
    _esc,
    _format_steps,
    _prepare_report_image,
    _repro_badge,
    _repro_detail,
    _trust_rank,
)
from .privacy import safe_report


@safe_report
def render_html(
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
            steps_block = (
                '<div class="st"><strong>How it surfaced:</strong> '
                '<span class="ac">Captured by Argus\'s console/network listener while '
                "on this page — it fires from the page itself, so it is not tied to a "
                "specific user journey.</span></div>"
            )
        else:
            steps_block = (
                '<div class="st"><strong>Steps to reproduce:</strong><ol>'
                f"{_format_steps(bug.steps_to_reproduce)}</ol></div>"
            )
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
                f"{_esc(_redact(nl.get('url', '')))} → {nl.get('status', '')}</code></div>"
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
{f"<p>{_esc(_dd)}</p>" if (_dd := _dedup_description(bug.title, bug.description)) else ""}
<div class="bu">URL: {_esc(_redact(bug.url))}</div>{repro_detail}
{steps_block}
{console}{network}{ss}</div>"""

    pages = "".join(f"<li>{_esc(p)}</li>" for p in r.pages_visited)
    focus = (
        "".join(f"<li>{_esc(f)}</li>" for f in r.focus_areas)
        if r.focus_areas
        else "<li>General exploration</li>"
    )
    coverage = r.coverage or {}
    coverage_goals = coverage.get("goals", [])
    coverage_summary = coverage.get("summary", {})
    coverage_pages = coverage.get("pages", {})
    coverage_budget = coverage.get("time_budget", {})
    has_coverage = bool(
        coverage_goals
        or r.constraints
        or coverage_pages.get("discovered")
        or coverage_budget.get("minutes")
    )
    if has_coverage:
        goal_items = ""
        for goal in coverage_goals:
            status = goal.get("status", "untested")
            if status not in {"untested", "in_progress", "exercised", "blocked"}:
                status = "untested"
            evidence = goal.get("evidence") or ""
            detail = f'<div class="ce">{_esc(evidence)}</div>' if evidence else ""
            refs = goal.get("evidence_refs") or {}
            reference_groups = []
            if refs.get("urls"):
                values = "".join(f"<li><code>{_esc(url)}</code></li>" for url in refs["urls"])
                reference_groups.append(f"<strong>Tested URLs</strong><ul>{values}</ul>")
            if refs.get("actions"):
                values = "".join(
                    f"<li><code>{_esc(action.get('tool', 'action'))}</code>"
                    + (
                        f" — {_esc(action.get('description', ''))}"
                        if action.get("description")
                        else ""
                    )
                    + "</li>"
                    for action in refs["actions"]
                )
                reference_groups.append(f"<strong>Actions</strong><ul>{values}</ul>")
            if refs.get("verifications"):
                values = "".join(
                    f'<li><span class="cv {"cv-ok" if check.get("matches") else "cv-bad"}">'
                    f"{'MATCH' if check.get('matches') else 'MISMATCH'}</span> "
                    f"{_esc(check.get('expect', ''))} {_esc(check.get('target_text', ''))} "
                    f"@ <code>{_esc(check.get('url', ''))}</code></li>"
                    for check in refs["verifications"]
                )
                reference_groups.append(f"<strong>Verification</strong><ul>{values}</ul>")
            if refs.get("findings"):
                finding_items = []
                for finding in refs["findings"]:
                    if finding.get("kind") == "bug":
                        trust = "verified" if finding.get("verified") else "unverified"
                        label = f"bug · {finding.get('severity', '')} · {trust}"
                    else:
                        label = f"observation · {finding.get('category', '')}"
                    finding_items.append(
                        f"<li>{_esc(label)}: {_esc(finding.get('title', ''))}</li>"
                    )
                values = "".join(finding_items)
                reference_groups.append(f"<strong>Linked findings</strong><ul>{values}</ul>")
            if refs.get("screenshots"):
                values = "".join(
                    f"<li>{_esc(screenshot.get('name', 'screenshot'))}</li>"
                    for screenshot in refs["screenshots"]
                )
                reference_groups.append(f"<strong>Screenshots</strong><ul>{values}</ul>")
            omitted = refs.get("omitted") or {}
            omitted_total = sum(value for value in omitted.values() if isinstance(value, int))
            if omitted_total:
                labels = ", ".join(f"{count} {kind}" for kind, count in omitted.items() if count)
                reference_groups.append(
                    f'<div class="co">Compact view omits {labels}; counts remain explicit.</div>'
                )
            references = (
                '<div class="cr">' + "".join(reference_groups) + "</div>"
                if reference_groups
                else ""
            )
            goal_items += (
                f'<li><span class="cs cs-{status}">{_esc(status.replace("_", " "))}</span> '
                f"<strong>{_esc(goal.get('goal', ''))}</strong>{detail}{references}</li>"
            )
        if not goal_items:
            goal_items = "<li>No explicit goals supplied; general exploration.</li>"
        constraints_html = ""
        if r.constraints:
            items = "".join(f"<li>{_esc(item)}</li>" for item in r.constraints)
            constraints_html = f"<h3>Constraints</h3><ul>{items}</ul>"
        unvisited_html = ""
        unvisited = coverage_pages.get("unvisited", [])
        if unvisited:
            items = "".join(f"<li>{_esc(path)}</li>" for path in unvisited)
            unvisited_html = f"<h3>Discovered but not visited</h3><ul>{items}</ul>"
        budget_html = ""
        if coverage_budget.get("minutes"):
            state = "exceeded" if coverage_budget.get("exceeded") else "observed"
            budget_html = (
                f'<p class="cb">Time budget: {coverage_budget["minutes"]} minutes ({state}); '
                f"{coverage_budget.get('elapsed_seconds', 0):.1f}s elapsed.</p>"
            )
        coverage_section = (
            "<h2>Coverage Contract</h2>"
            f'<p class="cb">{coverage_summary.get("exercised", 0)}/'
            f"{coverage_summary.get('total', 0)} goals exercised; "
            f"{coverage_summary.get('blocked', 0)} blocked; "
            f"{coverage_summary.get('in_progress', 0)} in progress; "
            f"{coverage_summary.get('untested', 0)} untested.</p>"
            f"<ul>{goal_items}</ul>{constraints_html}{unvisited_html}{budget_html}"
        )
    else:
        coverage_section = f"<h2>Focus Areas</h2><ul>{focus}</ul>"
    no_bugs = '<div class="nb">No bugs found.</div>' if not r.bugs else ""

    observations_html = ""
    if r.observations:
        observations_html = f"<h2>Review Observations ({len(r.observations)})</h2>"
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
.cb{{color:#94a3b8;margin:.5rem 0}}
.ce{{color:#94a3b8;margin:.25rem 0 .5rem}}
.cr{{background:#0f172a;border-left:2px solid #475569;border-radius:4px;margin:.5rem 0 1rem;padding:.7rem .9rem;color:#94a3b8}}
.cr strong{{color:#cbd5e1;font-size:.82rem}}
.cr ul{{margin:.25rem 0 .6rem 1.25rem;font-size:.82rem}}
.cr code{{color:#bfdbfe}}
.co{{font-size:.75rem;font-style:italic;color:#64748b}}
.cv{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:.65rem;font-weight:700}}
.cv-ok{{background:#166534;color:#dcfce7}}
.cv-bad{{background:#9a3412;color:#ffedd5}}
.cs{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.7rem;font-weight:700;text-transform:uppercase;margin-right:.35rem}}
.cs-exercised{{background:#166534;color:#dcfce7}}
.cs-blocked{{background:#9a3412;color:#ffedd5}}
.cs-in_progress{{background:#1d4ed8;color:#dbeafe}}
.cs-untested{{background:#334155;color:#cbd5e1}}
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
{coverage_section}
<h2>Bugs ({len(r.bugs)})</h2><div class="sm">{summary}</div>
{cards}{no_bugs}
{observations_html}
{screenshots_html}
<h2>Pages Visited</h2><ul>{pages}</ul>
</div></body></html>"""
