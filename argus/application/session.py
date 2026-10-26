from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from argus.application.context import current_runtime
from argus.domain.contracts import ErrorCode, OperationError, Rejection
from argus.domain.replay import InputVault

from .guidance import ExplorationProgress

if TYPE_CHECKING:
    from argus.ports import BrowserPort
from argus.domain.detector import Detector
from argus.domain.models import (
    Bug,
    Observation,
    Screenshot,
    new_run_id,
)


class Session:
    """Holds the state for one testing session.

    A session has a `mode` of either "web" (BrowserDriver) or "screen"
    (ScreenBackend). Tools that span both modes (record_bug,
    end_session) check `mode`; tools that are mode-specific (observe
    vs screen_observe) live as separate tools so the contract stays
    honest about what each mode can do.
    """

    def __init__(self):
        self.run_id = new_run_id()
        self.mode: Optional[str] = None  # "web" | "screen" | None
        self.review_mode: str = "exploratory"
        self.browser: Optional[BrowserPort] = None
        self.screen = None  # type: Optional["ScreenBackend"]
        self.detector = Detector()
        self.bugs: List[Bug] = []
        self.observations: List[Observation] = []
        self.steps: List[str] = []
        # Index into self.steps marking where the *last* record_bug call
        # snapshotted from. Each Bug's reproducible steps are the delta
        # since this cursor, so consecutive bug reports don't accumulate
        # earlier bugs' actions in their repro steps.
        self._steps_since_last_bug: int = 0
        self.pages_visited: List[str] = []
        self.screenshots: List[Screenshot] = []
        self.start_time: Optional[float] = None
        self.url: Optional[str] = None
        self.focus_areas: List[str] = []
        self.coverage_goals: List[dict] = []
        self.constraints: List[str] = []
        self.time_budget_minutes: int = 0
        self.discovered_pages: List[str] = []
        self.verification_checks: List[dict] = []
        self.exploration = ExplorationProgress()
        self._coverage_goal_cursors: dict[int, dict] = {}
        self._coverage_evidence_cursor: dict = {
            "actions": 0,
            "pages": 0,
            "screenshots": 0,
            "verifications": 0,
            "bugs": 0,
            "observations": 0,
        }
        self.tool_calls = 0
        self._last_elements = []
        self._last_screen_elements = []
        self._screenshot_counter = 0
        # Structured, replayable action trace (parallel to the human-readable
        # self.steps). Input values use in-memory references. record_bug slices
        # the actions since the previous bug onto Bug.replay_steps.
        self.action_trace: List[dict] = []
        self.inputs = InputVault()
        self.coverage_action_trace: List[dict] = []
        self._actions_since_last_bug: int = 0
        # Liveness marker of the most recently restored state capsule (or None).
        # record_bug re-checks it against the CURRENT page so a finding made
        # while the capsule's server session is dead gets flagged — and one made
        # after re-minting state through the UI does NOT (no sticky latch).
        self._capsule_marker: Optional[str] = None
        # Lazy-initialised screen-mode safety state. Populated by
        # start_screen_session; consulted by every screen-mode tool.
        self._safety = None  # type: Optional["argus.screen.safety.SafetyState"]

    @property
    def active(self) -> bool:
        return self.browser is not None or self.screen is not None


def _require_session() -> Session:
    if not current_runtime().session.active:
        raise OperationError(
            ErrorCode.NO_SESSION,
            "No active session. Call start_session(url) for web mode "
            "or start_screen_session() for screen mode first.",
        )
    current_runtime().session.tool_calls += 1
    return current_runtime().session


def _require_web_session(s: "Session", tool: str) -> Optional[str]:
    """Return an error string if this isn't a live web session, else None.

    Replaces the `if s.mode != "web" or s.browser is None: return "..."` guard
    that was copy-pasted across ~30 tools (the duplication that once let a
    missing guard slip through). Tools that also need an open page keep their
    explicit `_page is None` check."""
    if s.mode != "web" or s.browser is None:
        return Rejection(f"{tool}: this tool is web-mode only.")
    return None


async def _teardown_active_session() -> list[str]:
    """Cleanly stop whichever backend the current session holds.

    Both start_session and start_screen_session switch modes; tearing down
    only `.browser` crashed when the prior session was screen-mode
    (None.stop()), and only `.screen` would leak the browser. Stop whatever
    is set, best-effort.
    """
    return await current_runtime().close_backends()
