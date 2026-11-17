from __future__ import annotations

import asyncio

from argus.application.context import current_runtime
from argus.domain.contracts import Rejection, SessionState, ToolText
from argus.domain.coverage import FinalCoverageUpdate
from argus.domain.models import (
    ExplorationResult,
)

from .coverage import _apply_coverage_updates, _coverage_snapshot
from .evidence import _journal_confirmed, _output_dir
from .progress import Stage
from .session import Session


async def _write_report(s: Session, warnings: list[str]) -> tuple[ExplorationResult, str]:
    duration = asyncio.get_event_loop().time() - (s.start_time or 0)
    target = s.url if s.mode == "web" else f"screen://{s.mode}"
    result = ExplorationResult(
        url=target or "",
        bugs=s.bugs,
        pages_visited=s.pages_visited,
        actions_taken=len(s.steps),
        duration_seconds=duration,
        focus_areas=s.focus_areas,
        screenshots=s.screenshots,
        observations=s.observations,
        tool_calls=s.tool_calls,
        review_mode=s.review_mode,
        constraints=s.constraints,
        coverage=_coverage_snapshot(s, duration),
        run_id=s.run_id,
    )
    reporter = current_runtime().reporter_factory()
    try:
        report_path = await asyncio.to_thread(reporter.generate, result, _output_dir())
        warnings.extend(reporter.warnings)
    except Exception as exc:
        report_path = ""
        warnings.append(f"report was not saved: {type(exc).__name__}: {str(exc)[:160]}")
    return result, report_path


def _unfinished_goals(coverage: dict) -> list[str]:
    return [
        goal["goal"]
        for goal in coverage.get("goals", [])
        if goal["status"] in {"untested", "in_progress"}
    ]


async def save_abandoned_session() -> dict | None:
    """Save what a session that was never ended had found, before it is replaced.

    A session stopped by a timeout or a crashed browser keeps its findings in
    memory; starting another one used to discard them. Returns what was saved
    and which goals were left, or None when there was nothing to keep.
    """
    s = current_runtime().session
    goals = _unfinished_goals(_coverage_snapshot(s, 0)) if s.start_time is not None else []
    if s.start_time is None or not (s.bugs or s.observations or goals):
        return None
    saved: dict = {
        "run_id": s.run_id,
        "findings": len(s.bugs),
        "observations": len(s.observations),
        "unfinished_goals": goals,
        "report_path": None,
        "warnings": [],
    }
    if s.bugs or s.observations:
        warning = await _journal_confirmed(s)
        if warning:
            saved["warnings"].append(warning)
        _, report_path = await _write_report(s, saved["warnings"])
        saved["report_path"] = report_path or None
    return saved


def abandoned_session_note(saved: dict | None) -> str:
    if not saved:
        return ""
    lines = [f"The previous session ({saved['run_id']}) was not ended."]
    if saved["report_path"]:
        lines.append(
            f"Its {saved['findings']} finding(s) and {saved['observations']} observation(s) "
            f"are saved: {saved['report_path']}"
        )
    elif saved["findings"] or saved["observations"]:
        lines.append("Its report could not be saved: " + " | ".join(saved["warnings"]))
    if saved["unfinished_goals"]:
        lines.append(
            "Goals it left unfinished (pass them as goals to continue): "
            + "; ".join(saved["unfinished_goals"])
        )
    return "\n".join(lines) + "\n"


async def end_session(coverage_updates: list[FinalCoverageUpdate] | None = None) -> str:
    """End the testing session, close the browser, and generate an HTML error report.

    Returns the path to the generated report and a summary of findings.

    Args:
        coverage_updates: Optional final goal updates, each with goal (text or
            list), status (exercised or blocked) and concrete evidence. All
            updates validate before any change or shutdown. Unlisted goals keep
            their status; exercised does not mean passed. Invalid updates leave
            the session open. Use this instead of a separate final coverage_update.
    """
    s = current_runtime().session
    if not s.active and s.start_time is None:
        if coverage_updates:
            return Rejection("end_session: no active session for coverage updates.")
        return "No active session. Session resources are already closed."
    s.tool_calls += 1
    if coverage_updates is not None:
        error = _apply_coverage_updates(s, coverage_updates, final=True)
        if error:
            return Rejection(error.replace("coverage_update:", "end_session:", 1))
        if coverage_updates:
            await current_runtime().progress(Stage.COVERAGE)
    current_runtime().transition(SessionState.STOPPING)
    warnings: list[str] = []

    if s.mode == "web" and s.browser is not None:
        dropped = {name: count for name, count in s.browser.capture_drops().items() if count}
        if dropped:
            summary = ", ".join(f"{name}={count}" for name, count in dropped.items())
            warnings.append(f"capture limits discarded oldest events ({summary})")

    journal_warning = await _journal_confirmed(s)
    if journal_warning:
        warnings.append(journal_warning)

    teardown_errors = await current_runtime().close_backends()
    warnings.extend(teardown_errors)

    await current_runtime().progress(Stage.EXPORTING)
    result, report_path = await _write_report(s, warnings)
    duration, coverage = result.duration_seconds, result.coverage

    # Reset session
    if teardown_errors:
        current_runtime().transition(SessionState.FAILED)
    else:
        current_runtime().session = Session()

    # Build summary
    lines = [
        (
            f"Session ended. Report saved: {report_path}"
            if report_path
            else "Session ended, but the report could not be saved."
        ),
        "",
        "Summary:",
        f"  Tool calls: {result.tool_calls}",
        f"  Actions taken: {result.actions_taken}",
        f"  Pages visited: {len(result.pages_visited)}",
        f"  Bugs found: {len(result.bugs)}",
        f"  Observations: {len(result.observations)}",
        f"  Screenshots: {len(result.screenshots)}",
        f"  Duration: {duration:.1f}s",
    ]
    if warnings:
        lines.append("  Warnings: " + " | ".join(warnings))
    goal_summary = result.coverage.get("summary", {})
    if goal_summary.get("total"):
        lines.append(
            f"  Goals exercised: {goal_summary['exercised']}/{goal_summary['total']} "
            f"({goal_summary['blocked']} blocked)"
        )
        unfinished = _unfinished_goals(result.coverage)
        if unfinished:
            lines.append("  Unfinished goals: " + "; ".join(unfinished))
    unvisited = result.coverage.get("pages", {}).get("unvisited", [])
    if unvisited:
        lines.append("  Unvisited discovered pages: " + ", ".join(unvisited))
    if result.bugs:
        lines.append("")
        lines.append("Bugs:")
        for bug in result.bugs:
            lines.append(f"  [{bug.severity.value.upper()}] {bug.title}")

    return ToolText(
        "\n".join(lines),
        {
            "run_id": result.run_id,
            "report_path": report_path or None,
            "warnings": warnings,
            "coverage": coverage,
            "summary": {
                "tool_calls": result.tool_calls,
                "actions": result.actions_taken,
                "findings": len(result.bugs),
                "observations": len(result.observations),
                "duration_seconds": duration,
            },
        },
    )
