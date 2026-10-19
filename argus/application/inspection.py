from __future__ import annotations

import re

from argus.application.context import current_runtime

from .observation import _resolve_or_error
from .session import _require_session, _require_web_session


def _visual_inspection_query(description: str) -> tuple[str, str]:
    lowered = description.lower()
    kind = ""
    if "heading" in lowered:
        kind = "heading"
    elif any(word in lowered for word in ("image", "img", "icon", "logo")):
        kind = "image"
    elif any(word in lowered for word in (" text", "label", "status", "title")):
        kind = "text"

    quoted = re.findall(r'["“”]([^"“”]+)["“”]', description)
    if quoted:
        return max(quoted, key=len).strip(), kind

    removable = {"heading", "text", "label", "status", "image", "img", "icon", "logo", "element"}
    if kind == "heading":
        removable.update({"main", "page"})
    words = [
        word
        for word in description.split()
        if word.lower().strip("\"“”‘’'`()[]{}") not in removable
    ]
    query = " ".join(words).strip()
    return query or description, kind


def _format_inspected_element(description: str, info: dict, source: str) -> str:
    lines = [f"Inspecting {description!r} (resolved to <{info['tag']}> via {source})"]
    if info.get("matchScore") is not None:
        lines.append(f"Match confidence: {info['matchScore']}")
    lines.append("")
    lines.append("Visible text: " + (info.get("text") or "<none>")[:160])
    rect = info.get("rect", {})
    lines.append(
        f"Rect: x={rect.get('x', 0):.0f} y={rect.get('y', 0):.0f} "
        f"w={rect.get('width', 0):.0f} h={rect.get('height', 0):.0f} "
        f"in_viewport={rect.get('inViewport')}"
    )
    if info.get("truncated"):
        sd = info.get("scrollDimensions", {})
        lines.append(
            f"  TRUNCATED: scrollWidth={sd.get('scrollWidth')} > clientWidth={sd.get('clientWidth')} "
            f"(or scrollHeight > clientHeight) with overflow hidden — text is silently cut off."
        )
    if info.get("focused"):
        lines.append("Focus: this element currently has focus.")

    lines.append("")
    lines.append("Computed styles:")
    for key, value in (info.get("styles") or {}).items():
        lines.append(f"  {key}: {value}")

    lines.append("")
    lines.append("Accessibility:")
    lines.append(f"  role: {info.get('role') or '(default)'}")
    lines.append(f"  aria-label: {info.get('ariaLabel') or '(none)'}")
    lines.append(f"  aria-describedby: {info.get('ariaDescribedby') or '(none)'}")
    lines.append(f"  aria-hidden: {info.get('ariaHidden') or '(false/unset)'}")
    lines.append(f"  title: {info.get('title') or '(none)'}")
    lines.append(f"  disabled: {info.get('disabled')}, readonly: {info.get('readonly')}")
    if info.get("labels"):
        lines.append(f"  associated <label>(s): {info['labels']}")

    lines.append("")
    lines.append("outerHTML (first 1500 chars):")
    lines.append(info.get("outerHtml") or "(unavailable)")
    return "\n".join(lines)


async def inspect_element(description: str) -> str:
    """Get computed styles, ARIA metadata, and outerHTML for any visible element.

    Use this when you suspect a visual / a11y / truncation bug on a
    specific surface and observe()'s summary doesn't tell you enough.
    Returns:
      - rendered styles (color, background, font-size/weight, display,
        visibility, opacity, position, z-index, overflow, cursor, etc.)
      - bounding rect + whether it's in the viewport
      - whether the element is visually truncated by CSS (scrollWidth >
        clientWidth with overflow: hidden / text-overflow: ellipsis)
      - aria-label / aria-describedby / aria-hidden / role / title
      - associated <label> text(s)
      - disabled / readonly / focused state
      - first 1.5 KB of outerHTML

    Argus does not auto-judge anything from this output. You read it
    and decide whether anything you see warrants record_bug.
    """
    s = _require_session()
    mode_error = _require_web_session(s, "inspect_element")
    if mode_error:
        return mode_error
    el, err = _resolve_or_error(s, description)
    if el is not None:
        selector = s.browser.selector_for(el)
        info = await s.browser.inspect_element(selector)
        if info.get("found"):
            s.steps.append(f"inspect_element({description!r})")
            return _format_inspected_element(description, info, "interactive element")

    query, kind = _visual_inspection_query(description)
    info = await s.browser.inspect_visible_element(query, kind)
    if info.get("found"):
        s.steps.append(f"inspect_element({description!r})")
        return _format_inspected_element(description, info, "visible DOM")
    if info.get("ambiguous"):
        lines = [f"inspect_element({description!r}) is ambiguous — visible matches:"]
        for candidate in info.get("candidates", []):
            rect = candidate.get("rect", {})
            lines.append(
                f"  ({candidate.get('score')}) <{candidate.get('tag')}> "
                f"{candidate.get('text')!r} at x={rect.get('x', 0):.0f}, y={rect.get('y', 0):.0f}"
            )
        lines.append("Use the exact visible text or include the element kind/section.")
        return "\n".join(lines)
    return (
        err
        or f"No visible element matches {description!r}. Call observe() and rephrase the target."
    )


async def check_layout() -> str:
    """Inspect responsive layout signals without enabling arbitrary eval_js.

    Returns raw evidence for horizontal overflow, clipped text, undersized
    interactive targets, and fixed/sticky overlays. These are leads for visual
    review, not automatically-recorded bugs.
    """
    s = _require_session()
    mode_error = _require_web_session(s, "check_layout")
    if mode_error:
        return mode_error
    result = await s.browser.check_layout()
    if not result:
        return "check_layout: unable to inspect the current page."

    viewport = result.get("viewport", {})
    document = result.get("document", {})
    totals = result.get("totals", {})
    lines = [
        f"Layout signals for {s.browser.current_url}",
        f"  Viewport: {viewport.get('width')}x{viewport.get('height')} @ {viewport.get('devicePixelRatio')}x",
        f"  Document: {document.get('width')}x{document.get('height')}",
    ]
    groups = (
        ("Horizontal overflow", "horizontalOverflow"),
        ("Clipped text", "clippedText"),
        ("Targets below 44px", "smallTargets"),
        ("Fixed/sticky overlays", "fixedOrSticky"),
    )
    for label, key in groups:
        items = result.get(key, [])
        lines.append(f"\n{label}: {totals.get(key, len(items))}")
        for item in items[:8]:
            rect = item.get("rect", {})
            lines.append(
                f"  <{item.get('tag')}> {item.get('label')!r} "
                f"x={rect.get('x')} y={rect.get('y')} w={rect.get('width')} h={rect.get('height')}"
            )
    lines.append("\nTreat these as review leads; verify user impact before record_bug.")
    return "\n".join(lines)


async def eval_js(code: str) -> str:
    """Run arbitrary JavaScript in the page context and return the result.

    Disabled by default because it can read cookies, mutate state, and
    issue any fetch the page is allowed to. Enable with the `--unsafe`
    flag at server start (or `ARGUS_UNSAFE_EVAL=1`). Use this when:
      - inspecting state the standard tools don't expose (window.X,
        a global config object, IndexedDB contents);
      - resetting or seeding a test fixture via its own internal
        endpoints (`fetch('/api/test/reset', {method: 'POST'})`);
      - probing edge cases that require triggering JS the UI doesn't
        expose (race conditions, double-submit by direct fetch).

    The tool returns the JSON-serialised return value of the JS
    expression. If your code returns an unserialisable object, wrap
    it (e.g. `JSON.stringify(...)` or pluck specific fields).

    Argus does not auto-record bugs from eval_js output. If you find
    something via eval_js, call record_bug like with anything else.

    Args:
        code: A JS expression or arrow function. Examples:
            "() => window.location.href"
            "() => fetch('/api/test/reset', {method:'POST'}).then(r=>r.status)"
            "() => Object.keys(window.appConfig || {})"
    """
    if not current_runtime().settings.unsafe_eval:
        return (
            "eval_js is disabled. Restart argus-mcp with the --unsafe flag "
            "(or set ARGUS_UNSAFE_EVAL=1) to enable arbitrary JS execution. "
            "It is off by default because the JS you run has the same "
            "powers as the page itself."
        )

    s = _require_session()
    s.steps.append(f"eval_js: {code[:80]}")

    try:
        result = await s.browser.evaluate_js(code)
    except Exception as exc:
        return f"eval_js failed: {exc}"

    # Serialise. Playwright already gives us Python primitives for JSON-able
    # results. For other shapes, fall back to repr.
    try:
        import json as _json

        rendered = _json.dumps(result, default=str)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "... [truncated]"
        return f"eval_js result: {rendered}"
    except Exception:
        return f"eval_js result (repr): {result!r}"
