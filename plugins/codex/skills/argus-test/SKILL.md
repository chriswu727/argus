---
name: argus-test
description: Test a web app the user is building with Argus, from setup through verified findings. Use when the user asks to test, QA, or check a running web app or a feature they just built.
---

Test the application the user is authorized to exercise, using the Argus MCP
tools from this plugin.

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
uvx --python 3.12 --from argus-testing==0.11.0 --with playwright==1.64.1 python -m playwright install chromium
```

For other setup problems, run `uvx --python 3.12 --from argus-testing==0.11.0 argus-mcp --doctor`;
it prints the fix for each failed check. Then call `start_session` again.

## 3. Test

Follow the protocol that `start_session` returns. In short: mark the goals you
are about to walk `in_progress` with `coverage_update`, walk each journey with
real state, check persistence after writes with `verify_persistence`, and record
only reproducible, user-affecting bugs with `record_bug`. Use `verify` (or the
`suggested_verify` from a persistence mismatch) so Argus rechecks the symptom on
a fresh load. Use `record_observation` for visual or usability judgments.
Never repeat a write or use `replay` only to obtain a receipt.

## 4. Deliver

Close with `end_session`, passing the final `coverage_updates` for the goals you
exercised or found blocked, with the evidence you observed. Then report:

- Findings grouped by verdict: verified, observed (agent judgment), not
  reproduced, inconclusive. Never present an observed finding as verified.
- Coverage: goals exercised, blocked, and not reached.
- The report paths from `end_session`.

Completing the run does not mean the application passed. Do not change the
application's code unless the user asks; if they want fixes, offer to recheck
the findings afterwards with `regression_check`.
