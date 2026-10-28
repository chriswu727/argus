---
description: Test a web app the user is building with Argus, from setup through verified findings. Use when the user asks to test, QA, or check a running web app or a feature they just built.
argument-hint: "[url] [what to focus on]"
---

Test the application the user is authorized to exercise, using the Argus MCP
tools (`mcp__plugin_argus-testing_argus__*`). Arguments: $ARGUMENTS

## 1. Agree the scope

- Take the URL from the arguments or the conversation. If there is none, ask for it.
- Turn the request into a few concrete goals (user journeys) and constraints.
- Ask before testing anything with real-world effects: purchases, sending
  messages, deleting or publishing real data, or production systems. Absent
  permission, record such paths as blocked rather than exercising them.

## 2. Check the environment

Call `start_session` with the URL, goals and constraints. If it fails with a
browser-launch error, tell the user and offer to install the matching browser.
Run it only after they agree, because it downloads software:

```bash
VIRTUAL_ENV="${CLAUDE_PLUGIN_DATA}/venv" uv run --active --project "${CLAUDE_PLUGIN_ROOT}" --locked --python 3.12 python -m playwright install chromium
```

For other setup problems, run the same command with `argus-mcp --doctor` in
place of `python -m playwright install chromium`; it prints the fix for each
failed check. Then call `start_session` again.

## 3. Test

Follow the protocol that `start_session` returns. In short: mark the goals you
are about to walk `in_progress` with `coverage_update` and walk each journey with
real state. For a click that should change something (add, complete, delete,
save), use `test_action` with an `expect` prediction, such as
`{"removes": "Buy groceries"}` or `{"count": {"label": "tasks", "delta": 1}}`.
A SURPRISE is a lead to verify, not yet a bug.

Text alone hides many problems, so look at each key page as a user would at
least once per journey: take a `screenshot` and judge it. Watch for the main
action styled weaker than a secondary one, urgency, stock or discount claims
that the prices do not support, odd field order, labels that contradict each
other (a field marked required but labelled optional), and prices or totals
that disagree between pages. Run `check_layout` for overflow and clipped text.
Record such judgments with `record_observation`; use `record_bug` with `verify`
only when a text check on a fresh load can confirm the symptom.

Check persistence after writes
with `verify_persistence`, and record only reproducible, user-affecting bugs
with `record_bug`. Use `verify` (or the
`suggested_verify` from a persistence mismatch) so Argus rechecks the symptom on
a fresh load. Use `record_observation` for visual or usability judgments.
Never repeat a write or use `replay` only to obtain a receipt.

## 4. Deliver

Before closing, if the tool list includes `export_test`, call it for each
finding verified by a fresh-load text check. It writes a Playwright test that
fails while the bug is present and passes once it is fixed; give the user its
path so they can keep it in CI. Pass `run=true` only when the user authorized
repeating the journey's writes (for example on throwaway data), and report the
TODO steps it lists rather than guessing them.

Close with `end_session`, passing the final `coverage_updates` for the goals you
exercised or found blocked, with the evidence you observed. Then report:

- Findings grouped by verdict: verified, observed (agent judgment), not
  reproduced, inconclusive. Never present an observed finding as verified.
- Coverage: goals exercised, blocked, and not reached.
- The report paths from `end_session`.

Completing the run does not mean the application passed. Do not change the
application's code unless the user asks; if they want fixes, offer to recheck
the findings afterwards with `regression_check`.
