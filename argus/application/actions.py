from __future__ import annotations

from argus.domain.contracts import ErrorCode, Rejection

from .evidence import _record_action, _record_coverage_action
from .inspection import _visual_inspection_query
from .observation import (
    _begin_dialogs,
    _dialog_lines,
    _edge_case_hint,
    _finish_dialogs,
    _new_events_line,
    _new_tab_line,
    _resolve_or_error,
    _tab_count,
    _toast_line,
    _verify_nudge,
)
from .payloads import action_result
from .session import _require_session, _require_web_session


async def click_what(
    description: str, double_click: bool = False, accept_dialog: bool = False
) -> str:
    """Click the element best matching the natural-language `description`.

    Examples: "Login button", "Add Task", "the email field", "Delete near
    Buy groceries". A target description from observe() can be passed as-is.
    Argus matches against visible text, aria-label,
    placeholder, name, id, and the parent context. Trailing kind hints
    ("button" / "field" / "link" / "dropdown") narrow the candidate pool.

    If the description is ambiguous, this returns the top candidates with
    their distinguishing properties so you can rephrase. It does not
    guess and click — that's how testers misclick.

    double_click=True double-clicks instead, and may target plain visible text
    (an inline-edit label) by its exact wording. A JS alert/confirm/prompt the
    click raises is reported; it is dismissed unless accept_dialog=True, which
    accepts only that dialog. Accept only when the user authorized the action
    the dialog confirms.
    """
    s = _require_session()
    if double_click and s.mode == "web" and s.browser is not None:
        query, _ = _visual_inspection_query(description)
        mark = _begin_dialogs(s, accept_dialog)
        done = await s.browser.double_click_text(query)
        if done is not None:
            dialogs = _finish_dialogs(s, mark)
            if not done:
                return Rejection(
                    f"click_what({description!r}, double_click=true) — failed to "
                    f'double-click the text "{query[:60]}".',
                    ErrorCode.BACKEND,
                )
            s.steps.append(f'double-click text "{query[:60]}"')
            _record_action(
                s, "double_click", description, f"text:{query}", accept_dialog=accept_dialog
            )
            result = await action_result(
                s, f'Double-clicked the text "{query[:60]}".{_dialog_lines(dialogs)}', "click"
            )
            result.data["dialogs"] = dialogs
            return result
        _finish_dialogs(s, mark)
    el, err = _resolve_or_error(s, description)
    if err:
        return err

    label = el.text or el.aria_label or el.placeholder or el.name or el.id or el.tag
    step = f'click_what({description!r}) -> "{label[:60]}"'
    s.steps.append(step)

    # Peek error counts so we can surface any console/network error THIS click
    # triggers (see _new_events_line) without draining them from get_errors.
    _bc, _bn = s.browser.error_cursors()
    _bt = _tab_count(s)  # to detect a click that spawns a new tab
    mark = _begin_dialogs(s, accept_dialog)
    # Route through browser.click so a duplicate-label target hits the RESOLVED
    # element (nth-aware), not the first DOM match.
    index = s._last_elements.index(el)
    if double_click:
        ok = await s.browser.double_click(index, s._last_elements)
    else:
        ok = await s.browser.click(index, s._last_elements)
    dialogs = _finish_dialogs(s, mark)
    if not ok:
        return Rejection(
            f'click_what({description!r}) — failed to click "{label[:60]}".\n'
            "The element may be obscured, stale, or removed. Try observe() again.",
            ErrorCode.BACKEND,
        )
    _record_action(
        s,
        "double_click" if double_click else "click_what",
        description,
        accept_dialog=accept_dialog,
    )

    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    if new_state.url not in s.pages_visited:
        s.pages_visited.append(new_state.url)
    result = await action_result(
        s,
        f'{"Double-clicked" if double_click else "Clicked"} "{label[:60]}" '
        f"(via description {description!r}).\n"
        f"Now on: {new_state.url} — {len(new_state.elements)} interactive elements visible."
        f"{_toast_line(new_state.toast_messages)}"
        f"{_verify_nudge(label, bool(new_state.toast_messages))}"
        f"{_new_tab_line(s, _bt)}"
        f"{_new_events_line(s, _bc, _bn)}"
        f"{_dialog_lines(dialogs)}\n"
        f"Call observe() for a fresh view if needed.",
        "click",
        new_state,
    )
    result.data["dialogs"] = dialogs
    return result


async def type_into(description: str, text: str) -> str:
    """Type `text` into the input element best matching `description`.

    Examples: type_into("email", "alice@x.com"), type_into("confirm
    password", "...") , type_into("the search box", "buy"). Resolution
    rules are the same as click_what — see that tool for ambiguity behaviour.
    """
    s = _require_session()
    el, err = _resolve_or_error(
        s,
        description,
        kind_filter="input",
        strict_kind=True,
    )
    if err:
        return err

    label = el.placeholder or el.name or el.aria_label or el.id or el.tag
    s.steps.append(f"type_into({description!r}, ...) -> {label[:60]}")

    ok = await s.browser.type_text(s._last_elements.index(el), text, s._last_elements)
    if not ok:
        return Rejection(
            f"type_into({description!r}) — failed. "
            f"The element may be disabled or the page may have re-rendered.",
            ErrorCode.BACKEND,
        )
    _record_action(s, "type_into", description, text)
    return await action_result(
        s,
        f'Typed into "{label[:60]}" (via description {description!r}).{_edge_case_hint(s, el)}',
        "fill",
    )


async def paste_into(description: str, text: str) -> str:
    """Paste `text` into a field — fires the element's onpaste handler, then
    inserts the text if no handler consumed it.

    Different from type_into: pasting dispatches a `paste` ClipboardEvent (not
    per-key keydown/input), so it exercises paste-SPECIFIC logic — a coupon/token
    field that reformats on paste, an input that strips or rejects pasted content,
    a card or OTP field that splits a pasted value across boxes, a rich editor
    that sanitizes pasted HTML. Use type_into for normal typing; use paste_into to
    test paste behavior specifically. After it, observe() to see how the field
    handled the paste.
    """
    s = _require_session()
    err = _require_web_session(s, "paste_into")
    if err:
        return err
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    idx = s._last_elements.index(el)
    ok = await s.browser.paste_into(idx, text, s._last_elements)
    if not ok:
        return f"paste_into: failed on {description!r}."
    label = el.text or el.aria_label or el.placeholder or el.name or el.tag
    s.steps.append(f"paste_into({description!r}, ...)")
    _record_action(s, "paste_into", description, text)
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return f'Pasted into "{label[:50]}". Call observe() to see how the field handled the paste.'


async def select_into(description: str, value: str) -> str:
    """Select `value` in the dropdown best matching `description`."""
    s = _require_session()
    el, err = _resolve_or_error(
        s,
        description,
        kind_filter="select",
        strict_kind=True,
    )
    if err:
        return err

    label = el.aria_label or el.name or el.id or el.tag
    s.steps.append(f"select_into({description!r}, ...) -> {label[:60]}")

    ok = await s.browser.select_option(s._last_elements.index(el), value, s._last_elements)
    if not ok:
        return Rejection(
            f"select_into({description!r}, {value!r}) — failed. "
            f"Make sure the dropdown actually has that option "
            f"(call inspect_element to list the choices).",
            ErrorCode.BACKEND,
        )
    _record_action(s, "select_into", description, value)
    return await action_result(s, f'Selected "{value}" in "{label[:60]}".', "select")


async def hover_what(description: str) -> str:
    """Hover the mouse over the element best matching `description`.

    Real `:hover` (not synthetic): triggers tooltips, dropdown-on-hover
    menus, hover-only action buttons. Use after the element shows up
    in observe — for divs that observe filters out (figures, plain
    `<div>`s with `:hover` rules), introspect via inspect_element or
    fall back to eval_js.
    """
    s = _require_session()
    err = _require_web_session(s, "hover_what")
    if err:
        return err
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    label = el.text or el.aria_label or el.placeholder or el.id or el.tag
    s.steps.append(f"hover_what({description!r}) -> {label[:60]}")
    idx = s._last_elements.index(el)
    ok = await s.browser.hover(idx, s._last_elements)
    if not ok:
        return f"hover_what({description!r}) — failed (element may be off-screen or detached)."
    _record_action(s, "hover_what", description)
    # The observation is taken while the pointer stays put, so controls the
    # hover revealed (a row's delete button) are listed and targetable next.
    return await action_result(
        s, f'Hovered "{label[:60]}". Targets revealed by the hover are listed below.', "hover"
    )


async def right_click(description: str) -> str:
    """Right-click (button=right) the element best matching `description`.

    Use for custom context menus, "Open in new tab" tests, draggable
    handles that respond to right-click. Any context menu or alert that
    appears as a result is handled by the dialog queue (see
    set_dialog_handler).
    """
    s = _require_session()
    err = _require_web_session(s, "right_click")
    if err:
        return err
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    label = el.text or el.aria_label or el.placeholder or el.id or el.tag
    s.steps.append(f"right_click({description!r}) -> {label[:60]}")
    idx = s._last_elements.index(el)
    ok = await s.browser.right_click(idx, s._last_elements)
    if not ok:
        return f"right_click({description!r}) — failed."
    return f'Right-clicked "{label[:60]}". observe() to see the menu / new state.'


async def press_key(key: str, description: str = "") -> str:
    """Press a keyboard key — the web-mode equivalent testers reach for constantly.

    `key` is a Playwright key name: "Escape" (dismiss a modal/overlay), "Enter"
    (submit a focused field), "Tab" / "Shift+Tab" (keyboard navigation / focus
    order), "ArrowDown"/"ArrowUp"/"ArrowLeft"/"ArrowRight" (menus, sliders,
    listboxes), "Backspace", "Delete", "PageDown", "Home", or a chord like
    "Control+a". Optionally pass `description` to focus that element first
    (e.g. press ArrowRight on "the volume slider"); omit it to press at the
    page level (e.g. Escape to close whatever modal is open).

    Real users hit keys — a modal you can only close with Esc, a form that
    submits on Enter, arrow-key menu nav, focus-order bugs — none of which
    click/type can exercise. After it, observe() to see what changed.
    """
    s = _require_session()
    err = _require_web_session(s, "press_key")
    if err:
        return err
    target = ""
    if description:
        el, err = _resolve_or_error(s, description)
        if err:
            return err
        idx = s._last_elements.index(el)
        target = f' on "{(el.text or el.aria_label or el.placeholder or el.name or el.tag)[:50]}"'
        _bc, _bn = s.browser.error_cursors()
        ok = await s.browser.press_key(key, idx, s._last_elements)
    else:
        _bc, _bn = s.browser.error_cursors()
        ok = await s.browser.press_key(key)
    if not ok:
        return Rejection(
            f"press_key: failed to press {key!r} (unknown key name? use Playwright names like Escape/Enter/Tab/ArrowDown).",
            ErrorCode.BACKEND,
        )
    s.steps.append(f"press_key({key!r}{(', ' + repr(description)) if description else ''})")
    _record_action(s, "press_key", f"{key}{(' on ' + description) if description else ''}")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return await action_result(
        s,
        f"Pressed {key!r}{target}.{_new_events_line(s, _bc, _bn)}\n"
        f"Call observe() for a fresh view if needed.",
        "press",
        new_state,
    )


async def resize(width: int, height: int) -> str:
    """Resize the viewport mid-session to test RESPONSIVE layouts.

    Real users are on phones, tablets and desktops, and mobile-only bugs (a
    hamburger that never appears, content that doesn't reflow, an overlay that
    covers the page, tap targets that overlap) are exactly the class scripted E2E
    misses. Unlike opening a fresh session at a mobile width, this keeps your
    current state (logged in, cart filled, form typed) so you can compare the SAME
    page across breakpoints and test the transition itself. Common widths: 375
    (mobile), 414 (large phone), 768 (tablet), 1280/1440 (desktop). After it,
    observe() to see the reflowed layout.
    """
    s = _require_session()
    err = _require_web_session(s, "resize")
    if err:
        return err
    ok = await s.browser.resize(width, height)
    if not ok:
        return f"resize: failed to set viewport {width}x{height}."
    s.steps.append(f"resize({width}x{height})")
    _record_action(s, "resize", f"{width}x{height}")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return f"Viewport now {width}x{height}. Call observe() to see the responsive layout."


async def emulate_device(device: str) -> str:
    """Re-open the current page as a real MOBILE DEVICE — touch, mobile user-agent,
    device-pixel-ratio, and viewport — not just a viewport resize.

    resize() only changes width/height; many mobile bugs need the full device
    identity: touch-only interactions, mobile-only nav, content gated on a mobile
    UA, or a broken viewport-meta layout. Session state (cookies/login) carries
    over, so you can log in on desktop then switch to mobile. Common device names:
    "iPhone 13", "iPhone SE", "Pixel 5", "iPad Pro 11", "Galaxy S9+". Use resize()
    for a plain breakpoint sweep; use this for true device emulation. observe()
    after to see the mobile layout.
    """
    s = _require_session()
    err = _require_web_session(s, "emulate_device")
    if err:
        return err
    r = await s.browser.emulate_device(device)
    if not r.get("ok"):
        ex = r.get("examples") or []
        tail = f" Try one of: {', '.join(ex)}." if ex else ""
        return f"emulate_device: {r.get('reason', 'failed')}.{tail}"
    s.steps.append(f"emulate_device({device})")
    _record_action(s, "emulate_device", device)
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    vp = r.get("viewport") or {}
    return (
        f"Now emulating {device} — {vp.get('width')}x{vp.get('height')}, "
        f"touch={r.get('has_touch')}, mobile={r.get('is_mobile')}. "
        f"Call observe() to see the mobile layout."
    )


async def emulate_media(color_scheme: str = "", reduced_motion: str = "") -> str:
    """Emulate a media preference to test the app under it — DARK MODE
    (color_scheme="dark"), light ("light"), or reduced motion
    (reduced_motion="reduce").

    Dark-mode bugs — invisible text (same color as the dark background), unreadable
    contrast, icons/borders that vanish, a theme that only half-applies — are a
    class a manual pass routinely misses because you must toggle an OS/browser
    setting to even see them. Keeps page state. After it, screenshot()/observe() and
    judge whether the themed UI is still usable (an element you can't read IS a bug).
    """
    s = _require_session()
    err = _require_web_session(s, "emulate_media")
    if err:
        return err
    if not color_scheme and not reduced_motion:
        return 'emulate_media: pass color_scheme="dark"/"light" and/or reduced_motion="reduce".'
    ok = await s.browser.emulate_media(color_scheme or None, reduced_motion or None)
    if not ok:
        return "emulate_media: failed (valid: color_scheme dark|light|no-preference, reduced_motion reduce|no-preference)."
    what = ", ".join(
        x
        for x in (
            f"color_scheme={color_scheme}" if color_scheme else "",
            f"reduced_motion={reduced_motion}" if reduced_motion else "",
        )
        if x
    )
    s.steps.append(f"emulate_media({what})")
    _record_action(s, "emulate_media", what)
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return (
        f"Now emulating {what}. Call screenshot()/observe() to check the themed "
        f"UI is still readable and usable."
    )


async def click_at(x: int, y: int) -> str:
    """Click at viewport pixel (x, y) — the escape hatch for CANVAS/WebGL UIs.

    Charts, dashboards, maps (Mapbox/Leaflet), whiteboards (Excalidraw/tldraw/
    Figma), PDF.js viewers and WebGL games draw their UI as pixels with NO DOM
    elements — click_what/type_into can't target them. observe() flags any canvas
    region and its coordinates; call screenshot() to SEE the pixels, read off the
    coordinate of what you want, then click_at(x, y).

    For normal DOM pages ALWAYS prefer click_what (description-based) — pixel
    coordinates are brittle and only justified when the target is inside a canvas.
    """
    s = _require_session()
    err = _require_web_session(s, "click_at")
    if err:
        return err
    _bc, _bn = s.browser.error_cursors()
    ok = await s.browser.click_at(x, y)
    if not ok:
        return f"click_at: failed to click ({x},{y})."
    s.steps.append(f"click_at({x},{y})")
    _record_action(s, "click_at", f"({x},{y})")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return (
        f"Clicked at ({x},{y}).{_new_events_line(s, _bc, _bn)}\n"
        f"Call screenshot() to SEE the canvas result (observe() can't read pixels)."
    )


async def type_at(x: int, y: int, text: str) -> str:
    """Click (x, y) then type `text` — for a canvas-drawn text field with no DOM
    element to resolve. Coordinate escape hatch; prefer type_into on real DOM
    inputs. See click_at for the canvas workflow."""
    s = _require_session()
    err = _require_web_session(s, "type_at")
    if err:
        return err
    ok = await s.browser.type_at(x, y, text)
    if not ok:
        return f"type_at: failed at ({x},{y})."
    s.steps.append(f"type_at({x},{y},...)")
    _record_action(s, "type_at", f"({x},{y})")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return f"Typed at ({x},{y}). Call screenshot()/observe() to verify."


async def drag_at(from_x: int, from_y: int, to_x: int, to_y: int) -> str:
    """Drag by COORDINATES — press at (from_x,from_y), move to (to_x,to_y), release.

    The escape hatch for draggables Argus can't target by description: mouse-based
    sortable/reorder lists (Sortable.js, Trello, react-beautiful-dnd) whose items
    carry NO DOM marker, canvas drawing/resize, slider thumbs, map panning. Get
    coordinates from observe (a canvas region's center) or a screenshot. For a
    normal drag between two DESCRIBED elements, prefer drag_what — coordinates are
    brittle and only for what can't be targeted otherwise.
    """
    s = _require_session()
    err = _require_web_session(s, "drag_at")
    if err:
        return err
    ok = await s.browser.drag_at(from_x, from_y, to_x, to_y)
    if not ok:
        return f"drag_at: failed to drag ({from_x},{from_y}) -> ({to_x},{to_y})."
    s.steps.append(f"drag_at(({from_x},{from_y})->({to_x},{to_y}))")
    _record_action(s, "drag_at", f"({from_x},{from_y})->({to_x},{to_y})")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return (
        f"Dragged ({from_x},{from_y}) -> ({to_x},{to_y}). "
        f"Call observe()/screenshot() to see the result."
    )


async def hover_at(x: int, y: int) -> str:
    """Move the mouse to (x, y) — for a canvas hover tooltip / hover-reveal UI
    with no DOM element. Coordinate escape hatch; see click_at."""
    s = _require_session()
    err = _require_web_session(s, "hover_at")
    if err:
        return err
    ok = await s.browser.hover_at(x, y)
    if not ok:
        return f"hover_at: failed at ({x},{y})."
    s.steps.append(f"hover_at({x},{y})")
    _record_action(s, "hover_at", f"({x},{y})")
    return f"Moved mouse to ({x},{y}). Call screenshot() to see any tooltip/hover state."


async def drag_what(from_description: str, to_description: str) -> str:
    """Drag the element matching `from_description` onto `to_description`.

    Wraps Playwright's `page.drag_and_drop` (real mouse events — works
    for both HTML5 DnD and Sortable.js / dnd-kit / react-beautiful-dnd
    style mousedown+mousemove). Both endpoints must be visible in
    observe(); for `[draggable="true"]` divs that's automatic, for
    other element types the agent may need to scroll first.
    """
    s = _require_session()
    err = _require_web_session(s, "drag_what")
    if err:
        return err
    src, err = _resolve_or_error(s, from_description)
    if err:
        return f"drag_what(from): {err}"
    tgt, err = _resolve_or_error(s, to_description)
    if err:
        return f"drag_what(to): {err}"
    src_label = src.text or src.id or src.tag
    tgt_label = tgt.text or tgt.id or tgt.tag
    s.steps.append(f"drag_what({from_description!r} -> {to_description!r})")
    src_idx = s._last_elements.index(src)
    tgt_idx = s._last_elements.index(tgt)
    ok = await s.browser.drag(src_idx, tgt_idx, s._last_elements)
    if not ok:
        return (
            f"drag_what({from_description!r} -> {to_description!r}) — failed. "
            f"The source may not actually be draggable, or the target may be "
            f"off-screen."
        )
    return f'Dragged "{src_label[:40]}" onto "{tgt_label[:40]}". observe() to verify.'


async def upload_file(description: str, paths: list) -> str:
    """Attach one or more local files to the file `<input>` matching
    `description`.

    Wraps Playwright's `set_input_files` — works on both visible and
    hidden file inputs (most modern UIs hide the real input behind a
    styled label). For drag-drop upload zones that don't have an
    underlying `<input type=file>`, this won't work; use drop_file.

    Args:
        description: Match the file input. "file", "upload", or the
                     visible label text.
        paths: List of absolute paths to files to attach. Single file:
               pass a one-element list.
    """
    s = _require_session()
    err = _require_web_session(s, "upload_file")
    if err:
        return err
    if not paths:
        return "upload_file: paths is empty."
    el, err = _resolve_or_error(s, description, kind_filter="input")
    if err:
        return err
    if (el.type or "").lower() != "file":
        return (
            f"upload_file: matched element is `<input type={el.type!r}>`, "
            f"not a file input. Pass a description that matches the file "
            f"input specifically."
        )
    s.steps.append(f"upload_file({description!r}, {len(paths)} file(s))")
    idx = s._last_elements.index(el)
    ok = await s.browser.upload_file(idx, paths, s._last_elements)
    if not ok:
        return f"upload_file({description!r}) — failed."
    _record_coverage_action(s, "upload_file", f"{description}; {len(paths)} file(s)")
    return f"Attached {len(paths)} file(s) to {description!r}: {', '.join(paths)}"


async def drop_file(description: str, path: str) -> str:
    """Drop a real file onto a DROPZONE — the modern drag-and-drop upload area
    (react-dropzone / a <div> listening for drop events) that is NOT an
    <input type=file>.

    upload_file only works on a real file input; drag_what does an element-to-
    element mouse drag that never attaches a file (and misleadingly reports
    success on a dropzone). This dispatches a synthetic drop carrying the ACTUAL
    bytes of `path`, so the dropzone's handler receives a genuine File (name +
    size + contents) — letting you test upload validation, size/type limits, and
    preview rendering. After it, observe() to confirm the zone accepted it.

    Args:
        description: Match the dropzone (its visible text / label / role).
        path: Absolute path to the file to drop.
    """
    s = _require_session()
    err = _require_web_session(s, "drop_file")
    if err:
        return err
    import os

    if not os.path.exists(path):
        return f"drop_file: file not found: {path}"
    el, err = _resolve_or_error(s, description)
    if err:
        return err
    idx = s._last_elements.index(el)
    ok = await s.browser.drop_file(idx, path, s._last_elements)
    if not ok:
        return f"drop_file: failed to drop {path} onto {description!r}."
    label = el.text or el.aria_label or el.name or el.tag
    s.steps.append(f"drop_file({os.path.basename(path)!r} -> {description!r})")
    _record_action(s, "drop_file", f"{os.path.basename(path)} onto {description}")
    new_state = await s.browser.get_state()
    s._last_elements = new_state.elements
    return (
        f'Dropped {os.path.basename(path)} onto "{label[:50]}". '
        f"Call observe() to verify the dropzone accepted the file."
    )


async def set_dialog_handler(action: str = "accept", text: str = "") -> str:
    """Queue a response for the next JS `alert` / `confirm` / `prompt`.

    Playwright's default is to auto-dismiss every dialog, which means
    `confirm()` always sees Cancel and `prompt()` always sees null.
    Call this BEFORE the click that triggers the dialog so the agent
    actually controls OK vs Cancel and the prompt input value.

    Args:
        action: "accept" (OK) or "dismiss" (Cancel).
        text: For prompt(), the text to type before accepting. Ignored
              for alert/confirm.
    """
    s = _require_session()
    err = _require_web_session(s, "set_dialog_handler")
    if err:
        return err
    if action not in ("accept", "dismiss"):
        return "set_dialog_handler: action must be 'accept' or 'dismiss'."
    s.browser.queue_dialog_response(action, text)
    s.steps.append(f"set_dialog_handler({action!r}, text=...)")
    return (
        f"Next dialog will be {action}ed"
        + (f" with text {text!r}" if text else "")
        + ". Trigger the action that fires the dialog now."
    )
