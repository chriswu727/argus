"""macOS screen-mode backend.

Driven by PyObjC against AppKit / Quartz / ApplicationServices, plus
`screencapture` for image capture. The product story is "Argus tests
anything on screen as if a senior human QA tester sat down at the
machine" — this module is what turns the agent's intent into actions
on whatever app happens to be foreground.

Design choices:

- AX (Accessibility) tree is the structured side of observation. We
  walk the tree breadth-first up to a small depth and enumerate
  elements with role, label, value, and screen-coordinate frame. The
  resolver then maps a natural-language description to one of these
  elements — same contract as web mode.
- Screenshots come from `/usr/sbin/screencapture` rather than the
  Quartz screen-capture API because it's simpler, faster, and produces
  files in the same shape the rest of Argus already understands.
- Clicks and keystrokes use `cliclick` (a tiny brew-installed binary)
  — a thin, battle-tested wrapper around CGEventPost that doesn't
  require us to ship our own event-posting code.
"""
from __future__ import annotations

import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


def _is_macos() -> bool:
    return platform.system() == "Darwin"


@dataclass
class ScreenElement:
    """One AX element flattened into something the resolver can score."""
    role: str
    role_description: str
    title: str
    value: str
    description: str
    enabled: bool
    focused: bool
    # Screen-space rect — origin is top-left in macOS retina pixels.
    x: int
    y: int
    width: int
    height: int
    # AX hierarchy path — useful for disambiguating "Submit" buttons.
    path: List[str] = field(default_factory=list)
    # The underlying AXUIElementRef so callers can act on this element.
    _ax_ref: object = None


@dataclass
class ScreenObservation:
    """Snapshot of what's on the user's screen right now."""
    foreground_app: str
    foreground_pid: int
    foreground_window_title: str
    screen_width: int
    screen_height: int
    elements: List[ScreenElement] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class ScreenBackend:
    """macOS screen-mode driver. Holds onto the foreground app handle so
    repeated observe/click calls hit the same target.
    """

    # Attribute names the AX walker reads. Loaded lazily so this module
    # imports cleanly on non-macOS.
    _AX = None

    def __init__(self) -> None:
        self._workspace = None
        self._app_pid: Optional[int] = None
        self._app_name: Optional[str] = None
        self._target_app_name: Optional[str] = None
        self._max_depth: int = 6
        self._max_elements: int = 200

    # ── lifecycle ────────────────────────────────────────────────────

    def _load_frameworks(self) -> None:
        if not _is_macos():
            raise RuntimeError(
                "Screen mode is macOS-only. Run on a Mac and install with "
                "`pip install argus-testing[mac]`."
            )
        if self._AX is not None:
            return
        try:
            import AppKit  # noqa: F401
            import Quartz  # noqa: F401
            import ApplicationServices as AX
        except ImportError as exc:
            raise RuntimeError(
                "PyObjC frameworks not available. Install with "
                "`pip install argus-testing[mac]`."
            ) from exc

        self._AX = AX
        from AppKit import NSWorkspace
        self._workspace = NSWorkspace.sharedWorkspace()

    async def start(self, target_app: Optional[str] = None) -> ScreenObservation:
        """Bind to the current foreground app (or `target_app` if given).

        `target_app` matches the localised app name as shown in the menu
        bar (e.g. "Safari", "Notes", "Cursor"). The backend resolves it
        to a running PID; if no match, raises RuntimeError.
        """
        self._load_frameworks()
        self._target_app_name = target_app
        return await self.observe()

    async def stop(self) -> None:
        """Nothing to release — AX refs are reference-counted by the
        ObjC runtime. Kept for parity with the web backend."""
        return None

    # ── observation ──────────────────────────────────────────────────

    def _find_target_app(self):
        """Resolve target app to a PID + name + AXUIElement.

        Defaults to the frontmost app if no explicit target was set.
        When `target_app_name` is given, prefer an *exact* (case-
        insensitive) localised-name match, then bundle-id match, then
        substring match. This matters for cases like a "Unity" target
        when both "Unity" (the editor) and "Unity Hub" are running —
        the substring "unity" is in both, but the user almost certainly
        means the editor.
        """
        AX = self._AX
        if self._target_app_name:
            target = self._target_app_name
            target_lower = target.lower()
            running = list(self._workspace.runningApplications())
            exact = []
            bundle = []
            substring = []
            for app in running:
                name = app.localizedName() or ""
                bid = app.bundleIdentifier() or ""
                if name.lower() == target_lower:
                    exact.append((app, name))
                elif bid.lower() == target_lower or bid.lower().endswith("." + target_lower):
                    bundle.append((app, name))
                elif target_lower in name.lower() or target_lower in bid.lower():
                    substring.append((app, name))

            chosen = None
            if exact:
                chosen = exact[0]
            elif bundle:
                chosen = bundle[0]
            elif len(substring) == 1:
                chosen = substring[0]
            elif len(substring) > 1:
                names = ", ".join(repr(n) for _, n in substring)
                raise RuntimeError(
                    f"Screen mode: target {target!r} is ambiguous — "
                    f"matches {names}. Use the exact localised name "
                    f"(e.g. {substring[0][1]!r}) or pass the bundle id "
                    f"(e.g. {substring[0][0].bundleIdentifier()!r})."
                )

            if chosen is not None:
                app, name = chosen
                return (
                    app.processIdentifier(),
                    name,
                    AX.AXUIElementCreateApplication(app.processIdentifier()),
                )

            front = self._workspace.frontmostApplication()
            raise RuntimeError(
                f"Screen mode: no running app matches {target!r}. "
                f"Frontmost is {front.localizedName()!r}."
            )
        # No target specified — bind to whatever is foreground right now.
        front = self._workspace.frontmostApplication()
        return (
            front.processIdentifier(),
            front.localizedName() or "<unknown>",
            AX.AXUIElementCreateApplication(front.processIdentifier()),
        )

    def _ax_get(self, ref, attr: str):
        AX = self._AX
        try:
            err, value = AX.AXUIElementCopyAttributeValue(ref, attr, None)
            if err != 0:
                return None
            return value
        except Exception:
            return None

    def _flatten(self, ref, depth: int, path: List[str], out: List[ScreenElement]) -> None:
        """Walk the AX tree depth-first, capped by depth and total
        element count. Recursion is unconditional (containers like
        AXSplitGroup carry no identifying text but their children
        are exactly what we want to surface) — we only gate on
        whether an element is *recorded* in `out`.
        """
        if len(out) >= self._max_elements or depth > self._max_depth:
            return
        AX = self._AX

        role = self._ax_get(ref, AX.kAXRoleAttribute) or ""
        role_desc = self._ax_get(ref, AX.kAXRoleDescriptionAttribute) or ""
        title = self._ax_get(ref, AX.kAXTitleAttribute) or ""
        value = self._ax_get(ref, AX.kAXValueAttribute) or ""
        desc_text = self._ax_get(ref, AX.kAXDescriptionAttribute) or ""
        enabled = bool(self._ax_get(ref, AX.kAXEnabledAttribute) or False)
        focused = bool(self._ax_get(ref, AX.kAXFocusedAttribute) or False)

        rect = self._ax_rect(ref)
        x, y, w, h = rect or (0, 0, 0, 0)

        identifying_text = title or value or desc_text
        interactive_roles = {
            "AXButton", "AXLink", "AXTextField", "AXTextArea", "AXCheckBox",
            "AXRadioButton", "AXPopUpButton", "AXMenuItem", "AXMenuBarItem",
            "AXTab", "AXSlider", "AXComboBox", "AXStaticText",
        }
        is_interactive = role in interactive_roles
        # Record an element if it has text identity OR is intrinsically
        # interactive. Container roles (AXSplitGroup, AXGroup, AXScrollArea,
        # AXOutline, etc.) are skipped from the *output* but we still
        # recurse into their children below.
        if (identifying_text or is_interactive) and (w > 0 and h > 0):
            out.append(ScreenElement(
                role=role,
                role_description=role_desc,
                title=str(title),
                value=str(value)[:120],
                description=str(desc_text),
                enabled=enabled,
                focused=focused,
                x=int(x), y=int(y), width=int(w), height=int(h),
                path=list(path),
                _ax_ref=ref,
            ))

        children = self._ax_get(ref, AX.kAXChildrenAttribute)
        if not children:
            return
        # AX values can come back as floats / AXValueRefs in some apps
        # (sliders, scroll bars). Stringify defensively so the path
        # stays joinable.
        crumb = identifying_text or role or "?"
        next_path = path + [str(crumb)]
        for child in children:
            if len(out) >= self._max_elements:
                return
            self._flatten(child, depth + 1, next_path, out)

    def _ax_rect(self, ref) -> Optional[Tuple[float, float, float, float]]:
        """Read AXPosition + AXSize attributes and unpack to (x, y, w, h).

        PyObjC's AXValueGetValue takes (value, type, None) and returns
        (ok, unpacked_struct). The unpacked struct is a Quartz CGPoint
        / CGSize with .x .y / .width .height attributes.
        """
        AX = self._AX
        pos_val = self._ax_get(ref, AX.kAXPositionAttribute)
        size_val = self._ax_get(ref, AX.kAXSizeAttribute)
        if pos_val is None or size_val is None:
            return None
        try:
            ok_p, pt = AX.AXValueGetValue(pos_val, AX.kAXValueCGPointType, None)
            ok_s, sz = AX.AXValueGetValue(size_val, AX.kAXValueCGSizeType, None)
            if not (ok_p and ok_s):
                return None
            return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))
        except Exception:
            return None

    def _focused_window(self, app_ref):
        AX = self._AX
        focused = self._ax_get(app_ref, AX.kAXFocusedWindowAttribute)
        if focused is not None:
            return focused
        # Fall back to first window.
        windows = self._ax_get(app_ref, AX.kAXWindowsAttribute) or []
        return windows[0] if windows else app_ref

    async def observe(self, screenshot_dir: Optional[str] = None) -> ScreenObservation:
        """Capture a screenshot + AX tree of the foreground / target app."""
        self._load_frameworks()
        AX = self._AX

        pid, name, app_ref = self._find_target_app()
        self._app_pid = pid
        self._app_name = name

        window_ref = self._focused_window(app_ref)
        window_title = self._ax_get(window_ref, AX.kAXTitleAttribute) or ""

        # Walk a small slice of the tree.
        elements: List[ScreenElement] = []
        if window_ref is not None:
            self._flatten(window_ref, depth=0, path=[name, window_title], out=elements)

        # Screenshot the *target app's* window specifically — not the whole
        # screen. Falls back to whole-screen screencapture if we can't
        # resolve a window for this PID (e.g. the app has no on-screen
        # windows because it's minimised).
        ss_path = self._capture_window(pid, screenshot_dir) or self._capture(screenshot_dir)

        # Screen size — useful context for resolving coordinates / fold.
        from AppKit import NSScreen
        main = NSScreen.mainScreen()
        frame = main.frame() if main else None
        sw = int(frame.size.width) if frame else 0
        sh = int(frame.size.height) if frame else 0

        return ScreenObservation(
            foreground_app=name,
            foreground_pid=pid,
            foreground_window_title=str(window_title),
            screen_width=sw,
            screen_height=sh,
            elements=elements,
            screenshot_path=ss_path,
        )

    def _capture(self, screenshot_dir: Optional[str]) -> Optional[str]:
        """Whole-screen screencapture. Used as the fallback when window-
        targeted capture is unavailable."""
        out_dir = Path(screenshot_dir) if screenshot_dir else Path("argus-reports/screenshots")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        out_path = out_dir / f"screen_{ts}.png"
        try:
            subprocess.run(
                ["screencapture", "-t", "png", "-x", str(out_path)],
                capture_output=True, timeout=5, check=True,
            )
            return str(out_path)
        except Exception:
            return None

    def _capture_window(
        self, pid: int, screenshot_dir: Optional[str],
    ) -> Optional[str]:
        """Capture the largest visible on-screen window owned by `pid`.

        Uses Quartz CGWindowListCreateImage so the capture works even
        when the target app is *not* foreground — important because
        Argus drives the target via AX without disturbing focus, so
        the user's mouse-attended app is usually NOT the test target.
        Returns the saved PNG path on success, None on failure.
        """
        try:
            import Quartz
            from Foundation import NSURL, NSData
            import AppKit
        except ImportError:
            return None

        try:
            # Use kCGWindowListOptionAll (not OnScreenOnly) so we still
            # find the target's main window when it's hidden behind
            # another app. CGWindowListCreateImage can capture an off-
            # screen window's contents directly.
            window_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
        except Exception:
            return None

        # Pick the largest *named* window owned by `pid` and on the main
        # window layer. A "named" window is the user-visible title-bar
        # one — Unity creates several layer-0 windows per editor instance
        # (toolbars, scene-view chrome, "Hold on..." dialogs); only the
        # main editor window has a `kCGWindowName` set.
        best = None
        best_area = 0
        for w in window_list:
            if w.get("kCGWindowOwnerPID") != pid:
                continue
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            if not w.get("kCGWindowName"):
                continue
            bounds = w.get("kCGWindowBounds") or {}
            width = bounds.get("Width", 0)
            height = bounds.get("Height", 0)
            area = width * height
            if area <= 10_000:
                continue
            if area > best_area:
                best_area = area
                best = w

        if best is None:
            return None
        win_id = best.get("kCGWindowNumber")
        if win_id is None:
            return None

        try:
            # Note: kCGWindowListOptionIncludingWindow returns None for some
            # apps' background / non-foreground windows (empirically observed
            # on macOS 14 with Notes & Unity Editor). kCGWindowListOptionAll
            # combined with the explicit window id reliably resolves to the
            # target window's pixels regardless of focus state.
            cg_image = Quartz.CGWindowListCreateImage(
                Quartz.CGRectNull,
                Quartz.kCGWindowListOptionAll,
                int(win_id),
                Quartz.kCGWindowImageBoundsIgnoreFraming,
            )
            if cg_image is None:
                return None

            out_dir = Path(screenshot_dir) if screenshot_dir else Path("argus-reports/screenshots")
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            out_path = out_dir / f"window_{ts}.png"

            # CGImage -> NSBitmapImageRep -> PNG bytes -> file
            rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
            png_data = rep.representationUsingType_properties_(
                AppKit.NSBitmapImageFileTypePNG, None,
            )
            url = NSURL.fileURLWithPath_(str(out_path))
            ok = png_data.writeToURL_atomically_(url, True)
            if not ok:
                return None
            return str(out_path)
        except Exception:
            return None

    # ── interaction ──────────────────────────────────────────────────

    def click(self, el: ScreenElement) -> Tuple[bool, str]:
        """Click `el` — try AX press first, fall back to coordinate click.

        Returns (success, method_used). AX press is preferred because
        it's atomic and doesn't disturb the user's mouse position; we
        use coordinate clicks only when AX refuses (some apps don't
        implement AXPress on every clickable, e.g. Electron apps).
        """
        AX = self._AX

        # Try AXPress first — works for most native buttons.
        try:
            err = AX.AXUIElementPerformAction(el._ax_ref, AX.kAXPressAction)
            if err == 0:
                return True, "ax-press"
        except Exception:
            pass

        # Fall back to coordinate click via cliclick.
        cx = el.x + el.width // 2
        cy = el.y + el.height // 2
        try:
            subprocess.run(
                ["cliclick", f"c:{cx},{cy}"],
                capture_output=True, timeout=3, check=True,
            )
            return True, f"cliclick-coord:{cx},{cy}"
        except FileNotFoundError:
            return False, "cliclick-missing"
        except subprocess.CalledProcessError as exc:
            return False, f"cliclick-failed: {exc.stderr!r}"
        except Exception as exc:
            return False, f"click-error: {exc}"

    def type_into(self, el: ScreenElement, text: str) -> Tuple[bool, str]:
        """Focus `el` and type `text`.

        Strategy: try setting AXValue directly first (cleanest for
        AXTextField / AXTextArea — survives any focus quirks). If
        that's refused, focus the element and synthesize keystrokes
        via cliclick.
        """
        AX = self._AX

        # Path 1: AX value set. Works for most native text controls.
        try:
            err = AX.AXUIElementSetAttributeValue(
                el._ax_ref, AX.kAXValueAttribute, text,
            )
            if err == 0:
                return True, "ax-value"
        except Exception:
            pass

        # Path 2: focus + cliclick keystrokes.
        try:
            AX.AXUIElementSetAttributeValue(
                el._ax_ref, AX.kAXFocusedAttribute, True,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["cliclick", f"t:{text}"],
                capture_output=True, timeout=10, check=True,
            )
            return True, "cliclick-keystrokes"
        except FileNotFoundError:
            return False, "cliclick-missing"
        except subprocess.CalledProcessError as exc:
            return False, f"cliclick-failed: {exc.stderr!r}"
        except Exception as exc:
            return False, f"type-error: {exc}"

    def press_key(self, key: str) -> Tuple[bool, str]:
        """Press a single key by cliclick name (e.g. 'return', 'esc',
        'space', 'arrow-up'). Useful for submitting forms or dismissing
        modals when the agent has no clickable surface for it."""
        try:
            subprocess.run(
                ["cliclick", f"kp:{key}"],
                capture_output=True, timeout=3, check=True,
            )
            return True, f"cliclick-kp:{key}"
        except Exception as exc:
            return False, f"key-error: {exc}"

    # ── coordinate-driven primitives ──────────────────────────────────
    # Required when the target app is AX-blind (Unity, Electron with
    # custom rendering, Adobe self-render, web-canvas tools, etc.).
    # The caller resolves "what to click" by reading the screenshot
    # itself (vision LLM); these primitives just execute the action.

    def click_at(
        self,
        x: int,
        y: int,
        button: str = "left",
        count: int = 1,
        hold_ms: int = 0,
    ) -> Tuple[bool, str]:
        """Click at absolute screen coordinates.

        button: "left" | "right" | "middle"
        count:  1 (single), 2 (double), 3 (triple), or N for rapid
                consecutive single clicks (race-condition probing).
        hold_ms: when > 0, hold the button down for that many ms
                 before releasing (long-press / press-and-hold).
        """
        button_l = (button or "left").lower()
        if hold_ms > 0:
            # Mouse-down at coord, wait, mouse-up — long-press / hold.
            try:
                subprocess.run(
                    ["cliclick", f"dd:{x},{y}", f"w:{int(hold_ms)}", f"du:{x},{y}"],
                    capture_output=True, timeout=max(5, int(hold_ms / 1000) + 5),
                    check=True,
                )
                return True, f"cliclick-hold:{x},{y},{hold_ms}ms"
            except Exception as exc:
                return False, f"click_at-hold-error: {exc}"

        # Map count + button to a cliclick verb.
        if count == 1:
            verb = {"left": "c", "right": "rc", "middle": "c"}.get(button_l, "c")
        elif count == 2 and button_l == "left":
            verb = "dc"
        elif count == 3 and button_l == "left":
            verb = "tc"
        else:
            # N rapid clicks → repeat the single-click verb.
            verb = "rc" if button_l == "right" else "c"

        try:
            if count > 3 or (count > 1 and verb in ("c", "rc")):
                # Rapid sequence — chain N click commands in one cliclick call.
                args = ["cliclick"] + [f"{verb}:{x},{y}"] * count
                subprocess.run(args, capture_output=True, timeout=5, check=True)
                return True, f"cliclick-{verb}:{x},{y} x{count}"
            subprocess.run(
                ["cliclick", f"{verb}:{x},{y}"],
                capture_output=True, timeout=3, check=True,
            )
            return True, f"cliclick-{verb}:{x},{y}"
        except FileNotFoundError:
            return False, "cliclick-missing (brew install cliclick)"
        except subprocess.CalledProcessError as exc:
            return False, f"click_at-failed: {exc.stderr!r}"
        except Exception as exc:
            return False, f"click_at-error: {exc}"

    def hover_at(self, x: int, y: int) -> Tuple[bool, str]:
        """Move the cursor to (x, y) without clicking — useful for
        triggering hover-state changes the agent then observes."""
        try:
            subprocess.run(
                ["cliclick", f"m:{x},{y}"],
                capture_output=True, timeout=3, check=True,
            )
            return True, f"cliclick-m:{x},{y}"
        except Exception as exc:
            return False, f"hover_at-error: {exc}"

    def drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration_ms: int = 300,
    ) -> Tuple[bool, str]:
        """Press at (from_x, from_y), move to (to_x, to_y), release.

        duration_ms is split between the press-and-move phase. Apps that
        rely on inertial drag detection (sliders, kanban boards) don't
        always honour zero-duration drags, so 300 ms is the default.
        """
        try:
            args = [
                "cliclick",
                f"dd:{from_x},{from_y}",
                f"w:{int(duration_ms)}",
                f"m:{to_x},{to_y}",
                f"du:{to_x},{to_y}",
            ]
            subprocess.run(
                args, capture_output=True,
                timeout=max(5, int(duration_ms / 1000) + 5), check=True,
            )
            return True, f"cliclick-drag:{from_x},{from_y}->{to_x},{to_y}"
        except FileNotFoundError:
            return False, "cliclick-missing (brew install cliclick)"
        except Exception as exc:
            return False, f"drag-error: {exc}"

    def press_keys(self, keys: List[str]) -> Tuple[bool, str]:
        """Press a sequence of keys in order. Each item is a cliclick
        key name (e.g. 'return', 'esc', 'space', 'tab', 'arrow-down')
        or a combo (e.g. 'cmd-s', 'cmd-shift-z')."""
        if not keys:
            return False, "press_keys: empty key list"
        try:
            args = ["cliclick"] + [f"kp:{k}" for k in keys]
            subprocess.run(args, capture_output=True, timeout=10, check=True)
            return True, f"cliclick-kp:[{','.join(keys)}]"
        except FileNotFoundError:
            return False, "cliclick-missing (brew install cliclick)"
        except Exception as exc:
            return False, f"press_keys-error: {exc}"

    def type_at(self, x: int, y: int, text: str) -> Tuple[bool, str]:
        """Click at (x, y) to focus, then type `text`. Useful for AX-blind
        text fields where set-AXValue is unavailable."""
        ok, click_method = self.click_at(x, y)
        if not ok:
            return False, f"type_at: focus click failed ({click_method})"
        try:
            subprocess.run(
                ["cliclick", f"t:{text}"],
                capture_output=True, timeout=10, check=True,
            )
            return True, f"cliclick-c:{x},{y}+t:({len(text)} chars)"
        except FileNotFoundError:
            return False, "cliclick-missing (brew install cliclick)"
        except Exception as exc:
            return False, f"type_at-error: {exc}"

    # ── visual settle detection ────────────────────────────────────

    def wait_for_stable(
        self,
        timeout_s: float = 5.0,
        threshold_pct: float = 0.5,
        stable_window_ms: int = 400,
        poll_ms: int = 150,
        screenshot_dir: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str], dict]:
        """Poll the target window until it stops changing.

        Returns (settled, reason, final_screenshot_path, stats).

        After an action (click / drag / launch), the screen is usually
        in motion: loading spinner, animation, layout reflow. The
        agent's job is to wait until the dust settles before reading
        the result. Sleeping for an arbitrary N ms is brittle — fast
        machines waste time, slow machines fire too early. `wait_for_stable`
        polls a screenshot every `poll_ms` and computes a Pillow pixel
        diff against the previous frame; once `stable_window_ms` worth
        of consecutive frames stay below `threshold_pct`, the page is
        considered settled.

        Falls back gracefully:
        - if PIL isn't available, returns (False, "no-pil", None, {})
        - if the target window can't be found at all, returns
          (False, "no-window", None, {})
        - if the timeout fires before stability is observed, returns
          (False, "timeout", last_screenshot, {...frames, last_diff})
        """
        from PIL import Image, ImageChops

        deadline = time.time() + timeout_s
        last_path = None
        last_image = None
        stable_for_ms = 0
        frames = 0
        last_diff_pct = 0.0

        # Take an initial baseline.
        last_path = self._capture_window(self._app_pid or -1, screenshot_dir)
        if last_path is None or not Path(last_path).exists():
            # Whole-screen fallback, then continue.
            last_path = self._capture(screenshot_dir)
        if last_path is None or not Path(last_path).exists():
            return False, "no-window", None, {}

        try:
            last_image = Image.open(last_path).convert("RGB")
        except Exception as exc:
            return False, f"baseline-open-error: {exc}", last_path, {}

        while time.time() < deadline:
            time.sleep(poll_ms / 1000.0)
            frames += 1
            new_path = self._capture_window(self._app_pid or -1, screenshot_dir)
            if new_path is None or not Path(new_path).exists():
                new_path = self._capture(screenshot_dir)
            if new_path is None:
                continue
            try:
                new_image = Image.open(new_path).convert("RGB")
            except Exception:
                continue

            # Resize the smaller to match if window changed size.
            if new_image.size != last_image.size:
                target_size = (
                    min(new_image.size[0], last_image.size[0]),
                    min(new_image.size[1], last_image.size[1]),
                )
                new_image = new_image.resize(target_size)
                last_image = last_image.resize(target_size)

            diff = ImageChops.difference(new_image, last_image).convert("L")
            mask = diff.point(lambda v: 255 if v > 25 else 0, mode="L")
            changed = sum(1 for px in mask.getdata() if px > 0)
            total = mask.size[0] * mask.size[1]
            last_diff_pct = (changed / total * 100) if total else 0.0

            if last_diff_pct < threshold_pct:
                stable_for_ms += poll_ms
                if stable_for_ms >= stable_window_ms:
                    return True, "settled", new_path, {
                        "frames": frames,
                        "last_diff_pct": round(last_diff_pct, 3),
                        "elapsed_s": round(time.time() - (deadline - timeout_s), 2),
                    }
            else:
                stable_for_ms = 0

            last_path = new_path
            last_image = new_image

        return False, "timeout", last_path, {
            "frames": frames,
            "last_diff_pct": round(last_diff_pct, 3),
            "stable_for_ms": stable_for_ms,
        }

    # ── app lifecycle ──────────────────────────────────────────────

    def _running_app_matching(self, target: str):
        """Return the first NSRunningApplication matching `target`
        (case-insensitive localised name OR bundle id). None if not running."""
        if self._workspace is None:
            self._load_frameworks()
        target_lower = (target or "").lower()
        for app in self._workspace.runningApplications():
            name = (app.localizedName() or "").lower()
            bundle = (app.bundleIdentifier() or "").lower()
            if name == target_lower or bundle == target_lower:
                return app
            if target_lower and (target_lower in name or target_lower in bundle):
                return app
        return None

    def is_running(self, target: str) -> Tuple[bool, Optional[int]]:
        """Is `target` (localised name or bundle id) currently running?"""
        self._load_frameworks()
        app = self._running_app_matching(target)
        if app is None:
            return False, None
        return True, int(app.processIdentifier())

    def launch(self, target: str, wait_s: float = 8.0) -> Tuple[bool, str, Optional[int]]:
        """Launch `target` (an app name, bundle id, or absolute path).

        If the app is already running, returns its existing pid.
        Otherwise launches via `open` (most permissive — accepts names,
        bundle ids, and paths) and polls until the new process appears
        or `wait_s` elapses.

        The poll spins the current NSRunLoop briefly each iteration —
        without that, NSWorkspace.runningApplications() returns a
        stale snapshot in long-lived Python processes (PyObjC quirk:
        new-app notifications need run-loop ticks to register).
        """
        self._load_frameworks()
        import Foundation

        existing = self._running_app_matching(target)
        if existing is not None:
            return True, "already-running", int(existing.processIdentifier())

        # `open -b <bundle>` is more reliable for bundle ids than `open -a`.
        looks_like_bundle = "." in target and "/" not in target
        cmd = ["open", "-b", target] if looks_like_bundle else ["open", "-a", target]
        try:
            subprocess.run(
                cmd, capture_output=True, timeout=10, check=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            return False, f"{' '.join(cmd)} failed: {exc.stderr.strip()[:160]}", None
        except Exception as exc:
            return False, f"launch-error: {exc}", None

        loop = Foundation.NSRunLoop.currentRunLoop()
        deadline = time.time() + wait_s
        while time.time() < deadline:
            # Pump the run loop briefly so NSWorkspace processes the
            # app-launched notification. Without this, runningApplications()
            # returns a stale snapshot inside a long-lived process.
            loop.runUntilDate_(
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.2)
            )
            app = self._running_app_matching(target)
            if app is not None and app.isFinishedLaunching():
                return True, "launched", int(app.processIdentifier())

        # Last-chance check — `isFinishedLaunching` never flips for some
        # background utilities; accept if the process is at least alive.
        app = self._running_app_matching(target)
        if app is not None:
            return True, "launched (no finishedLaunching signal)", int(app.processIdentifier())
        return False, f"launched but did not register within {wait_s}s", None

    def quit(
        self,
        target: str,
        force: bool = False,
        wait_s: float = 8.0,
    ) -> Tuple[bool, str]:
        """Quit `target`. By default sends a polite terminate (like
        cmd-Q), which gives the app a chance to flush state — required
        if you're testing save-on-quit semantics. `force=True` sends a
        SIGKILL-equivalent for hung apps."""
        self._load_frameworks()
        app = self._running_app_matching(target)
        if app is None:
            return True, "not-running"

        try:
            if force:
                app.forceTerminate()
                method = "forceTerminate"
            else:
                app.terminate()
                method = "terminate"
        except Exception as exc:
            return False, f"quit-error: {exc}"

        import Foundation
        loop = Foundation.NSRunLoop.currentRunLoop()
        deadline = time.time() + wait_s
        while time.time() < deadline:
            loop.runUntilDate_(
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.2)
            )
            if self._running_app_matching(target) is None:
                return True, method
        return False, f"{method} sent but app still running after {wait_s}s"
