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
        """Resolve target app to a PID + name + AXUIElement. Defaults to
        the frontmost app if no explicit target was set."""
        AX = self._AX
        if self._target_app_name:
            running = self._workspace.runningApplications()
            for app in running:
                name = app.localizedName() or ""
                if name == self._target_app_name or self._target_app_name.lower() in name.lower():
                    return (
                        app.processIdentifier(),
                        name,
                        AX.AXUIElementCreateApplication(app.processIdentifier()),
                    )
            raise RuntimeError(
                f"Screen mode: no running app matches {self._target_app_name!r}. "
                f"Frontmost is {self._workspace.frontmostApplication().localizedName()!r}."
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
        next_path = path + [identifying_text or role or "?"]
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

        # Screenshot to a fresh path under the configured output dir.
        ss_path = self._capture(screenshot_dir)

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
