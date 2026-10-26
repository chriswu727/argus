from __future__ import annotations

import asyncio

from argus.application.context import current_runtime
from argus.domain.contracts import Rejection, SessionState, ToolText
from argus.domain.coverage import FinalCoverageUpdate
from argus.domain.models import (
    ExplorationResult,
)

from .coverage import _apply_coverage_updates, _coverage_snapshot
from .evidence import _output_dir, _write_journal
from .progress import Stage
from .session import Session


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

    if s.mode == "web":
        try:
            await asyncio.to_thread(_write_journal, s)
        except Exception as exc:
            warnings.append(f"journal was not saved: {type(exc).__name__}: {str(exc)[:160]}")

    teardown_errors = await current_runtime().close_backends()
    warnings.extend(teardown_errors)

    duration = asyncio.get_event_loop().time() - (s.start_time or 0)
    coverage = _coverage_snapshot(s, duration)

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
        coverage=coverage,
        run_id=s.run_id,
    )

    output_dir = _output_dir()
    reporter = current_runtime().reporter_factory()
    await current_runtime().progress(Stage.EXPORTING)
    try:
        report_path = await asyncio.to_thread(reporter.generate, result, output_dir)
        warnings.extend(reporter.warnings)
    except Exception as exc:
        report_path = ""
        warnings.append(f"report was not saved: {type(exc).__name__}: {str(exc)[:160]}")

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
        unfinished = [
            goal["goal"]
            for goal in result.coverage["goals"]
            if goal["status"] in {"untested", "in_progress"}
        ]
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
