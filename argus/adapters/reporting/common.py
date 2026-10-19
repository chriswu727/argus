from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from PIL import Image

from argus.domain.models import (
    Bug,
    BugType,
    Severity,
)

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
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
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
            return (
                '<span class="rp" style="background:#6e7781">'
                "AUTO-CAPTURED EVENT · not independently verified</span>"
            )
        return '<span class="rp" style="background:#6e7781">VERIFY NOT RUN · clause rejected</span>'
    reproduced = receipt.get("reproduced")
    is_replay = receipt.get("mode") == "replay"
    if reproduced is True:
        if is_replay:
            label = f"VERIFIED · reproduced by replaying {receipt.get('steps', '?')} steps from a cold start"
        else:
            runs = str(receipt.get("runs", "")).replace("/", " of ")
            label = (
                f"VERIFIED · reproduced on {runs} clean reloads"
                if runs.strip()
                else "VERIFIED · reproduced on a clean reload"
            )
        color = "#1a7f37"
    elif reproduced is False:
        if is_replay:
            color, label = (
                "#b35900",
                f"NOT REPRODUCED · replayed {receipt.get('steps', '?')} steps, symptom absent",
            )
        elif receipt.get("expect_status") is not None:
            color, label = "#b35900", f"NOT REPRODUCED · expected HTTP {receipt['expect_status']}"
        elif receipt.get("flaky"):
            color, label = "#9a6700", f"INTERMITTENT · {receipt.get('runs', '')} on reload"
        else:
            color, label = (
                "#b35900",
                "NOT REPRODUCED · absent on clean reload (may be intermittent — re-check)",
            )
    else:  # None — inconclusive
        if is_replay and receipt.get("diverged"):
            color, label = (
                "#6e7781",
                "INCONCLUSIVE · replay path diverged (a step no longer resolves)",
            )
        elif is_replay:
            color, label = (
                "#6e7781",
                "INCONCLUSIVE · symptom pre-existed the journey (not caused by these steps)",
            )
        elif receipt.get("reason"):
            color, label = "#6e7781", f"INCONCLUSIVE · {receipt['reason']}"
        elif receipt.get("error"):
            color, label = "#6e7781", "REPRO CHECK ERRORED"
        else:
            color, label = "#6e7781", "INCONCLUSIVE · no verdict"
    return f'<span class="rp" style="background:{color}">{_esc(label)}</span>'


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
            return (
                f'<div class="rd">Independent re-check was inconclusive: {_esc(str(reason))}.</div>'
            )
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
        body = f"Independently confirmed on a fresh load: <code>{_esc(tgt)}</code> is {state}."
    else:
        want = "present" if expect == "present" else "absent"
        body = (
            f"Could not independently confirm on a fresh load: "
            f"<code>{_esc(tgt)}</code> was expected {want}, but the re-check disagreed."
        )
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
    steps = [re.sub(r"^\s*\d+[.)]\s*", "", s).strip() for s in steps if s and s.strip()]
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
