"""export_test: turn a verified finding into a regression test file."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from argus.domain.contracts import ErrorCode, Rejection, ToolText
from argus.storage import StorageError, atomic_write_text

from .context import current_runtime
from .session import _require_session, _require_web_session
from .testgen import generate_test, run_exported_test


def _journey(s, end: int) -> list:
    """Recorded actions up to `end`, with each capsule restore in its place."""
    journey = list(s.action_trace[:end])
    for position, name in reversed(s.capsule_restores):
        if position <= end:
            journey.insert(position, {"tool": "capsule_restore", "description": name})
    return journey


async def export_test(finding_id: str, run: bool = False, save_private_values: bool = False) -> str:
    """Write a regression test for a verified finding of this session.

    The test replays the journey recorded up to the finding and asserts the
    opposite of the verified symptom on a fresh load: it fails while the bug is
    present and passes once it is fixed. It is a standalone Python file that
    uses Playwright, written under the report directory; copy it into the
    project's tests to keep it in CI.

    Only findings verified on a fresh load with a text check can be exported.
    Steps Argus cannot translate are marked TODO and block the test before any writes.
    Application URLs, inputs and locator/assertion text are read from environment variables;
    run=true passes this session's values to the test that way, never in the file.

    Args:
        finding_id: The finding_id that record_bug returned.
        save_private_values: Only when the user explicitly wants to retain local
            replay values, write a private JSON file (may contain credentials).
            Default false. Never publish it; use ARGUS_VALUES_FILE for later runs.
        run: Run the test once now to show that it fails on the symptom. This
            repeats the journey's writes against the app, so use it only when
            the user authorized repeating them (for example throwaway data).
            The run's outcome is fails_on_symptom, passes, inconclusive (the
            fresh load could not show the symptom either way), incomplete or
            errored.
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

    spec = bug.verification_spec
    if spec.get("version") != 1:
        return Rejection(
            "export_test: complete verification conditions are unavailable; record the finding again."
        )
    if spec.get("clear_storage"):
        return Rejection(
            "export_test: clear_storage verification is not supported by test export; the storage reset cannot be omitted."
        )

    directory = current_runtime().settings.output_dir / "tests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"test_argus_{s.run_id}_{index}.py"

    def resolve_input(action: dict):
        try:
            return s.inputs.resolve([action])[0].get("value")
        except ValueError:
            return None

    export_id = uuid.uuid4().hex
    secrets: dict = {}
    required_env: dict = {}
    source, notes = generate_test(
        finding_id=finding_id,
        title=bug.title,
        expected=bug.expected,
        receipt={**receipt, **spec},
        journey=_journey(s, bug.journey_end),
        start_url=s.url or receipt.get("at_url") or "",
        resolve_input=resolve_input,
        filename=path.name,
        secrets=secrets,
        required_env=required_env,
        export_id=export_id,
    )
    values_file = None
    private_dir = None
    try:
        if save_private_values:
            private_dir = Path(tempfile.mkdtemp(prefix=".argus-values-", dir=directory))
            atomic_write_text(private_dir / ".gitignore", "*\n")
            values_file = private_dir / "values.json"
            payload = {
                "export_id": export_id,
                "values": {
                    name: value for name, value in secrets.items() if name != "ARGUS_EXPORT_ID"
                },
            }
            atomic_write_text(values_file, json.dumps(payload, ensure_ascii=True, indent=2))
        atomic_write_text(path, source)
    except (OSError, StorageError):
        if private_dir is not None:
            shutil.rmtree(private_dir, ignore_errors=True)
        return Rejection(
            "export_test: could not save the export; check output directory permissions."
        )
    s.steps.append(f"export_test({finding_id!r}) -> {path.name}")

    lines = [f"Exported regression test for {finding_id}: {path}"]
    lines.extend(f"  {name}: {purpose}" for name, purpose in required_env.items())
    if values_file:
        lines.append(
            f"  Private values saved: {values_file}. Set ARGUS_VALUES_FILE to this path; never publish this file."
        )
    else:
        lines.append(
            "  Values were not saved. Configure them privately, or explicitly request save_private_values=true before closing the session."
        )
    if notes:
        lines.append(f"  {len(notes)} step(s) need manual translation (marked TODO in the file):")
        lines.extend(f"    {note}" for note in notes)
    result = None
    if run and notes:
        result = {"outcome": "incomplete", "detail": "Translate the listed steps before running."}
        lines.append(
            "  Not run: incomplete journey. Translate every TODO step and remove the guard."
        )
    elif run:
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
        elif outcome == "inconclusive":
            lines.append(
                f"  Ran once: INCONCLUSIVE ({result['detail']}). The fresh load could not show "
                "the symptom either way, so this run neither reproduces the bug nor shows it "
                "fixed. Under pytest an inconclusive check fails; re-run it once the page "
                "loads as it did when the finding was verified."
            )
        else:
            lines.append(f"  Ran once: errored before the check ({result['detail']}).")
    else:
        lines.append(
            "  Not run. run=true runs it once (repeating the journey's writes) to show it fails now."
        )
    if not notes:
        lines.append(
            "  Confirm it fails on the symptom before using it as a regression check in CI."
        )
    return ToolText(
        "\n".join(lines),
        {
            "finding_id": finding_id,
            "export_id": export_id,
            "path": str(path),
            "manual_steps": notes,
            "required_env": required_env,
            "private_values_path": str(values_file) if values_file else None,
            "run": result,
        },
    )
