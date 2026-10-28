"""export_test: turn a verified finding into a regression test file."""

from __future__ import annotations

from argus.domain.contracts import ErrorCode, Rejection, ToolText

from .context import current_runtime
from .session import _require_session, _require_web_session
from .testgen import _slug, generate_test, run_exported_test


def _journey(s, end: int) -> list:
    """Recorded actions up to `end`, with each capsule restore in its place."""
    journey = list(s.action_trace[:end])
    for position, name in reversed(s.capsule_restores):
        if position <= end:
            journey.insert(position, {"tool": "capsule_restore", "description": name})
    return journey


async def export_test(finding_id: str, run: bool = False) -> str:
    """Write a regression test for a verified finding of this session.

    The test replays the journey recorded up to the finding and asserts the
    opposite of the verified symptom on a fresh load: it fails while the bug is
    present and passes once it is fixed. It is a standalone Python file that
    uses Playwright, written under the report directory; copy it into the
    project's tests to keep it in CI.

    Only findings verified on a fresh load with a text check can be exported.
    Steps Argus cannot translate are marked TODO in the file and listed here.
    Sensitive inputs (passwords, tokens) are read from environment variables;
    run=true passes this session's values to the test that way, never in the file.

    Args:
        finding_id: The finding_id that record_bug returned.
        run: Run the test once now to show that it fails on the symptom. This
            repeats the journey's writes against the app, so use it only when
            the user authorized repeating them (for example throwaway data).
    """
    s = _require_session()
    err = _require_web_session(s, "export_test")
    if err:
        return err
    index = next(
        (i for i, _ in enumerate(s.bugs, 1) if finding_id == f"{s.run_id}:{i}"),
        None,
    )
    if index is None:
        return Rejection("export_test: finding_id must identify a finding in this session.")
    bug = s.bugs[index - 1]
    receipt = bug.reproduction_receipt or {}
    if receipt.get("reproduced") is not True or receipt.get("mode") == "replay":
        return Rejection(
            "export_test: only findings verified by a fresh-load text check can be exported; "
            "this one is not verified that way.",
            ErrorCode.INVALID_ARGUMENT,
        )
    if receipt.get("expect") not in ("present", "absent") or not receipt.get("target_text"):
        return Rejection(
            "export_test: status-code findings cannot be exported yet; "
            "only text-presence symptoms are supported.",
            ErrorCode.INVALID_ARGUMENT,
        )

    directory = current_runtime().settings.output_dir / "tests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"test_argus_{s.run_id}_{index}_{_slug(bug.title)[:40]}.py"

    def resolve_input(action: dict):
        try:
            return s.inputs.resolve([action])[0].get("value")
        except ValueError:
            return None

    secrets: dict = {}
    source, notes = generate_test(
        finding_id=finding_id,
        title=bug.title,
        expected=bug.expected,
        receipt=receipt,
        journey=_journey(s, bug.journey_end),
        start_url=s.url or receipt.get("at_url") or "",
        resolve_input=resolve_input,
        filename=path.name,
        secrets=secrets,
    )
    path.write_text(source, encoding="utf-8")
    s.steps.append(f"export_test({finding_id!r}) -> {path.name}")

    lines = [f"Exported regression test for {finding_id}: {path}"]
    if notes:
        lines.append(f"  {len(notes)} step(s) need manual translation (marked TODO in the file):")
        lines.extend(f"    {note}" for note in notes)
    result = None
    if run:
        # Finish inside the operation deadline; cancellation kills the test.
        budget = max(10.0, current_runtime().settings.operation_timeout - 15.0)
        result = await run_exported_test(path, timeout=min(180.0, budget), secrets=secrets)
        outcome = result["outcome"]
        if outcome == "fails_on_symptom":
            lines.append("  Ran once: FAILS on the symptom, as it should while the bug is present.")
        elif outcome == "passes":
            lines.append(
                "  Ran once: PASSES now, so it does not reproduce the bug. Do not rely on it "
                "as a regression check until the journey or the check is fixed."
            )
        else:
            lines.append(f"  Ran once: errored before the check ({result['detail']}).")
    else:
        lines.append(
            "  Not run. run=true runs it once (repeating the journey's writes) to show it fails now."
        )
    lines.append("  Copy it into the project's tests; it passes once the bug is fixed.")
    return ToolText(
        "\n".join(lines),
        {
            "finding_id": finding_id,
            "path": str(path),
            "manual_steps": notes,
            "run": result,
        },
    )
