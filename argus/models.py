from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


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
    disabled: bool = False
    role: Optional[str] = None
    aria_label: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None
    parent_context: Optional[str] = None


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


@dataclass
class Screenshot:
    path: str
    name: str
    step: str
    url: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ExplorationResult:
    url: str
    bugs: List[Bug]
    pages_visited: List[str]
    actions_taken: int
    duration_seconds: float
    focus_areas: List[str]
    screenshots: List[Screenshot] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
