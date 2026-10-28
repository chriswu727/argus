from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from argus.security import export_trace


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:6]}"


def validated_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value or ""):
        raise ValueError("run_id must contain only letters, numbers, dot, underscore, or hyphen")
    return value


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class BugType(str, Enum):
    CONSOLE_ERROR = "console_error"
    NETWORK_ERROR = "network_error"
    VISUAL_ANOMALY = "visual_anomaly"
    UX_ISSUE = "ux_issue"
    CRASH = "crash"
    BROKEN_LINK = "broken_link"
    FORM_ERROR = "form_error"
    STATE_VERIFICATION = "state_verification"
    MISLEADING_SUCCESS = "misleading_success"
    COUNT_MISMATCH = "count_mismatch"
    TEXT_ANOMALY = "text_anomaly"
    BROKEN_IMAGE = "broken_image"
    SEO_ISSUE = "seo_issue"
    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    MIXED_CONTENT = "mixed_content"


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    NAVIGATE = "navigate"
    SELECT = "select"
    SCROLL = "scroll"
    WAIT = "wait"
    BACK = "back"
    SUBMIT = "submit"
    DONE = "done"


@dataclass
class InteractiveElement:
    index: int
    tag: str
    type: Optional[str] = None
    text: Optional[str] = None
    placeholder: Optional[str] = None
    href: Optional[str] = None
    value: Optional[str] = None
    checked: Optional[bool] = None  # checkbox/radio/switch live checked state (None = N/A)
    aria_state: Optional[str] = None  # compact ARIA state: expanded/collapsed/pressed/current
    disabled: bool = False
    role: Optional[str] = None
    aria_label: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None
    parent_context: Optional[str] = None
    shadow: bool = False  # element lives inside an open shadow root
    frame: Optional[str] = None  # selector of the iframe this element lives in (None = main frame)
    # Structural CSS path for a form field with no id/name/placeholder/aria-label
    # attribute, whose aria_label may come from a nearby <label> instead.
    css_path: Optional[str] = None


@dataclass
class PageState:
    url: str
    title: str
    elements: List[InteractiveElement]
    page_text: str = ""
    toast_messages: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    css_indicators: List[str] = field(default_factory=list)
    item_lists: Dict[str, List[str]] = field(default_factory=dict)
    links: List[Dict] = field(default_factory=list)
    images: List[Dict] = field(default_factory=list)
    meta_tags: Dict[str, str] = field(default_factory=dict)
    headings: List[Dict] = field(default_factory=list)
    accessibility_issues: List[Dict] = field(default_factory=list)
    mixed_content: List[Dict] = field(default_factory=list)
    open_modals: List[Dict] = field(default_factory=list)
    canvases: List[Dict] = field(default_factory=list)  # pixel-drawn regions (canvas/WebGL)
    focused: Optional[Dict] = None
    viewport: Optional[Dict] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Action:
    type: ActionType
    reasoning: str = ""
    element_index: Optional[int] = None
    value: Optional[str] = None
    url: Optional[str] = None


@dataclass
class Bug:
    type: BugType
    severity: Severity
    title: str
    description: str
    url: str
    steps_to_reproduce: List[str]
    screenshot_path: Optional[str] = None
    console_logs: List[str] = field(default_factory=list)
    network_logs: List[Dict] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    raw_error: Optional[str] = None
    # Independent re-check of the bug's observable symptom from a clean load.
    # None = no machine-checkable symptom supplied (observation/visual finding).
    reproduction_receipt: Optional[Dict] = None
    # Structured action trace since the previous bug — the deterministic steps a
    # replay engine can re-drive (tool + description + value), parallel to the
    # human-readable steps_to_reproduce.
    replay_steps: List[Dict] = field(default_factory=list)
    verification_history: List[Dict] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    coverage_goals: List[str] = field(default_factory=list)
    claimed_evidence_calls: List[int] = field(default_factory=list)
    evidence_warnings: List[str] = field(default_factory=list)
    # Length of the session's action trace when the finding was recorded: the
    # journey export_test replays from the session start.
    journey_end: int = 0

    def to_dict(self) -> Dict:
        """Machine-readable finding — for JSON/JUnit/SARIF export so Argus can be
        consumed programmatically (CI gate, API, dashboard), not just read as HTML.
        Carries the receipt verdict so a consumer can filter to PROVEN findings."""
        r = self.reproduction_receipt or {}
        verdict = r.get("reproduced")  # True / False / None (inconclusive)
        return {
            "title": self.title,
            "severity": getattr(self.severity, "value", str(self.severity)),
            "type": getattr(self.type, "value", str(self.type)),
            "url": self.url,
            "description": self.description,
            "expected": self.expected,
            "actual": self.actual,
            "coverage_goals": list(self.coverage_goals),
            "claimed_evidence_calls": list(self.claimed_evidence_calls),
            "evidence_warnings": list(self.evidence_warnings),
            "steps_to_reproduce": list(self.steps_to_reproduce or []),
            "screenshot_path": self.screenshot_path,
            "replay_steps": export_trace(self.replay_steps or []),
            # Trust tier: the reproduction receipt is Argus's differentiator — a
            # consumer should be able to gate on PROVEN, not on say-so.
            "verified": verdict is True,
            "reproduction": dict(r) if r else None,
            "verification_history": list(self.verification_history),
            "console_logs": list(self.console_logs or []),
            "network_logs": list(self.network_logs or []),
            "timestamp": self.timestamp.isoformat()
            if hasattr(self.timestamp, "isoformat")
            else self.timestamp,
        }


@dataclass
class Screenshot:
    path: str
    name: str
    step: str
    url: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "name": self.name,
            "step": self.step,
            "url": self.url,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class Observation:
    title: str
    evidence: str
    url: str
    category: str = "visual"
    screenshot_path: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "evidence": self.evidence,
            "url": self.url,
            "category": self.category,
            "screenshot_path": self.screenshot_path,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ExplorationResult:
    url: str
    bugs: List[Bug]
    pages_visited: List[str]
    actions_taken: int
    duration_seconds: float
    focus_areas: List[str]
    screenshots: List[Screenshot] = field(default_factory=list)
    observations: List[Observation] = field(default_factory=list)
    tool_calls: int = 0
    review_mode: str = "exploratory"
    constraints: List[str] = field(default_factory=list)
    coverage: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    run_id: str = field(default_factory=new_run_id)
