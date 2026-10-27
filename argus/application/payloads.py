from __future__ import annotations

from argus.domain.contracts import ToolText
from argus.domain.models import PageState
from argus.resolver import describe

from .coverage import _coverage_snapshot, _update_coverage_from_state
from .guidance import next_checks
from .session import Session


async def action_result(session: Session, text: str, action: str, state=None) -> ToolText:
    state = state if state is not None else await session.browser.get_state()
    session._last_elements = state.elements
    if state.url not in session.pages_visited:
        session.pages_visited.append(state.url)
    _update_coverage_from_state(session, state)
    data = observation_data(session, state)
    data["action"] = {"kind": action, "executed": True, "application_passed": None}
    return ToolText(text, data)


def observation_data(session: Session, state: PageState, previous_counts=None, previous_url=None):
    comparable = previous_counts is not None and previous_url == state.url
    coverage = _coverage_snapshot(session)
    return {
        "page": {"url": state.url, "title": state.title[:300]},
        "visible_text": state.page_text[:1800],
        "targets": [
            {"description": describe(element)[:300], "disabled": element.disabled}
            for element in state.elements[:80]
        ],
        "counts": dict(list(state.counts.items())[:50]),
        "count_changes": {
            label: count - previous_counts[label]
            for label, count in list(state.counts.items())[:50]
            if comparable and label in previous_counts and count != previous_counts[label]
        },
        "feedback": [text[:300] for text in state.toast_messages[:10]],
        "coverage": coverage,
        "next_checks": next_checks(session, state, coverage),
        "omitted": {
            "targets": max(0, len(state.elements) - 80),
            "counts": max(0, len(state.counts) - 50),
            "feedback": max(0, len(state.toast_messages) - 10),
            "text_characters_in_legacy_content": max(0, len(state.page_text) - 1800),
        },
        "follow_up": ["Use inspect_element for target details or screenshot for visual evidence."],
    }
