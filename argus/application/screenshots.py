from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from argus.domain.contracts import ImageArtifact
from argus.domain.models import (
    Screenshot,
)

from .evidence import _screenshot_dir
from .observation import _resolve_or_error
from .session import _require_session


async def screenshot(
    name: str = "screenshot",
    element: str = "",
    full_page: bool = False,
) -> list:
    """Capture a screenshot — full viewport, full page, or one element.

    Use this whenever something looks visually off and you want evidence
    for a record_bug call, or when you want a before/after pair to feed
    into screenshot_diff.

    Args:
        name: Filename label (no extension).
        element: Optional element description (same syntax as click_what).
                 If given, crops the screenshot to that element's bounds.
                 Use for visual hierarchy / truncation / contrast checks.
        full_page: If True, capture the entire scrollable page rather
                   than just the viewport. Ignored when `element` is set.
    """
    s = _require_session()
    last_step = s.steps[-1] if s.steps else "Initial state"

    if element:
        el, err = _resolve_or_error(s, element)
        if err:
            return err
        s._screenshot_counter += 1
        safe_name = f"{s._screenshot_counter:03d}_elem_{name}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name)
        path = str(_screenshot_dir(s) / f"{safe_name}.png")
        selector = s.browser.selector_for(el)
        result = await s.browser.element_screenshot(selector, path)
        if result is None:
            return (
                f"screenshot(element={element!r}) — could not capture; the "
                f"element may have moved or detached. observe() and try again."
            )
        s.screenshots.append(
            Screenshot(
                path=path,
                name=safe_name,
                step=last_step,
                url=s.browser.current_url,
            )
        )
        absolute_path = str(Path(path).resolve())
        return [f"Element screenshot saved: {absolute_path}", ImageArtifact(path=absolute_path)]

    s._screenshot_counter += 1
    suffix = "_fullpage" if full_page else ""
    safe_name = f"{s._screenshot_counter:03d}_{name}{suffix}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name)
    path = str(_screenshot_dir(s) / f"{safe_name}.png")
    await s.browser.screenshot(path, full_page=full_page)
    s.screenshots.append(
        Screenshot(
            path=path,
            name=safe_name,
            step=last_step,
            url=s.browser.current_url,
        )
    )
    absolute_path = str(Path(path).resolve())
    return [f"Screenshot saved: {absolute_path}", ImageArtifact(path=absolute_path)]


async def screenshot_diff(
    before: str,
    after: str,
    name: str = "diff",
    threshold: int = 25,
) -> list:
    """Compare two screenshots and produce a third image with changed
    regions highlighted in red, so you can see what visually changed
    between two states.

    Useful for detecting layout shifts, content updates that should not
    have happened, focus-ring changes after a click, modal overlays
    appearing, theme switches, etc. Argus does not auto-judge whether
    a diff is a bug — you read the side-by-side and decide.

    Args:
        before: Path or filename of the earlier screenshot (returned
                from a previous `screenshot()` call).
        after: Path of the later screenshot.
        name: Label for the output diff image.
        threshold: 0-255 per-channel pixel difference above which a
                   pixel is considered "changed". Default 25 (mild).
                   Lower = more sensitive.
    """
    from PIL import Image, ImageChops, ImageDraw

    s = _require_session()
    out_dir = _screenshot_dir(s)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(p: str) -> Optional[Path]:
        candidates = [Path(p)]
        if not Path(p).is_absolute():
            candidates.append(out_dir / Path(p).name)
            candidates.append(Path("argus-reports/screenshots") / Path(p).name)
        for c in candidates:
            if c.exists():
                return c
        return None

    before_path = _resolve(before)
    after_path = _resolve(after)
    if before_path is None:
        return f"screenshot_diff: cannot find {before!r}"
    if after_path is None:
        return f"screenshot_diff: cannot find {after!r}"

    try:
        img_a = Image.open(before_path).convert("RGB")
        img_b = Image.open(after_path).convert("RGB")
    except Exception as exc:
        return f"screenshot_diff: failed to open images: {exc}"

    if img_a.size != img_b.size:
        # Resize the smaller to match — keeps comparison meaningful even
        # when viewport changed slightly.
        target = (
            min(img_a.size[0], img_b.size[0]),
            min(img_a.size[1], img_b.size[1]),
        )
        img_a = img_a.resize(target)
        img_b = img_b.resize(target)

    diff_img = ImageChops.difference(img_a, img_b)
    bbox = diff_img.getbbox()

    s._screenshot_counter += 1
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{s._screenshot_counter:03d}_{name}")
    out_path = out_dir / f"{safe_name}.png"

    if bbox is None:
        # Pixel-identical. Save before image as the diff so the agent has
        # a concrete artifact, but signal that there's no change.
        img_a.save(out_path)
        message = (
            f"screenshot_diff: images are pixel-identical — no visible change.\n"
            f"  Saved (copy of before): {out_path.resolve()}"
        )
        s.screenshots.append(
            Screenshot(
                path=str(out_path.resolve()),
                name=safe_name,
                step=f"diff: {before} vs {after}",
                url=s.browser.current_url,
            )
        )
        return [message, ImageArtifact(path=out_path)]

    # Build a binary mask of changed pixels at the requested threshold.
    grey = diff_img.convert("L")
    mask = grey.point(lambda v: 255 if v > threshold else 0, mode="L")

    # Composite a translucent red overlay onto the after image at the mask.
    red_layer = Image.new("RGB", img_b.size, (255, 0, 0))
    composite = Image.composite(red_layer, img_b, mask).convert("RGB")
    # Blend back so changed regions are tinted, not solid red.
    blended = Image.blend(img_b, composite, 0.5)

    # Outline the overall bounding box for orientation.
    draw = ImageDraw.Draw(blended)
    draw.rectangle(bbox, outline=(255, 0, 0), width=3)

    blended.save(out_path)

    # Heuristic: how much of the image changed?
    changed_pixels = sum(1 for px in mask.getdata() if px > 0)
    total_pixels = mask.size[0] * mask.size[1]
    pct = (changed_pixels / total_pixels) * 100 if total_pixels else 0

    s.screenshots.append(
        Screenshot(
            path=str(out_path.resolve()),
            name=safe_name,
            step=f"diff: {before} vs {after}",
            url=s.browser.current_url,
        )
    )

    message = (
        f"screenshot_diff saved: {out_path.resolve()}\n"
        f"  Changed bounding box: {bbox} (size {bbox[2] - bbox[0]} x {bbox[3] - bbox[1]})\n"
        f"  Approx {pct:.1f}% of pixels changed (threshold={threshold}).\n"
        f"  Decide: is this change expected for the action you took? If a UI region "
        f"changed unexpectedly, that may be a bug — call record_bug."
    )
    return [message, ImageArtifact(path=out_path)]
