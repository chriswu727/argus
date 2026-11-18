---
description: Test a running web app with Argus and report verified bugs with evidence. Use when the user asks to test, QA or check an app or a feature; when a development task has changed something a person uses in a browser and it should be checked there before the work is called done; and when a bug found earlier has been fixed and needs re-testing. Load it before asking the user where the app is or what to test: it says how to find out.
argument-hint: "[url] [what to focus on]"
---

Test the application the user is authorized to exercise, using the Argus MCP
tools (`mcp__plugin_argus-testing_argus__*`), and finish without needing the
user: they should hear the result, not manage the run. Arguments: $ARGUMENTS

Two rules hold for the whole run:

- Do not open by asking where the app is or what to test. Look first; section
  1 says where.
- Find and report. Do not edit the application's code unless the user asked
  for a fix, and never before you have closed the session and reported.

## 1. Settle the scope yourself

- **Address.** Take it from the arguments or the conversation. Otherwise find
  it: the dev server you or the user already started, the project's start
  script and port (`package.json`, `Makefile`, `docker-compose.yml`, the
  README, `.env`), or the framework default. If the app is not running and
  starting it is part of the work you were given, start it with the project's
  own command. Ask only when none of this gives an address.
- **Goals.** Turn the request into three to six user journeys. For "test the
  app", cover the main flows. For a feature that was just built or changed,
  cover that feature and the flows next to it. Each journey should include
  what a person would really do: the normal path, a wrong or empty input, a
  change of state (add, edit, delete, toggle), and a reload to see whether the
  change stayed.
- **Budget.** Pass `time_budget_minutes` (15 is a good default; less for one
  feature) and stop when it is spent. Report what was not reached.
- **Authority.** Stay inside what the user already allowed. Do not buy, send
  messages, publish, or delete real data, and do not test a production system,
  unless the user said so; record such a path as blocked and go on. Ask the
  user only for something you cannot get any other way: a login you were not
  given, or a decision outside the task.

## 2. Start

Call `start_session` with the address, goals, constraints and budget. Setup
does not need the user:

- The first session downloads the browser (one time, about 150 MB). If the
  result says the download is still running, wait a minute and call
  `start_session` again, for up to five minutes.
- If the browser still cannot start, install it into the environment the
  server runs in, then call `start_session` again:

  ```bash
  uv run --isolated --project "${CLAUDE_PLUGIN_ROOT}" --locked --python 3.12 python -m playwright install chromium
  ```

- If the address cannot be reached, the app is not running: start it as in
  section 1 and try again.
- For any other setup failure, run the same command with `argus-mcp --doctor` in place of
  `python -m playwright install chromium`.
  It prints the fix for each failed web check; the macOS screen grants it
  lists are not needed for web testing.

Tell the user about a setup problem only when the fix is theirs to make: no
network to download from, or system libraries that need administrator rights.

If the result says earlier findings are journaled for this site and the user
asked to re-test after a fix, go to section 5.

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

The two tools read `expect` differently. `verify_persistence` takes the state
that should hold (a deleted row is `absent`). `record_bug`'s `verify` takes the
symptom, what the page shows while the bug is there (the deleted row is still
`present`). When a persistence check fails, record it straight away, before
any other action, and name the affected text in the title or description.

Finding a bug is not a reason to stop or to start fixing. Record it, finish
the other journeys, close the session and report.

Report what you found, not how much. A page with no defects is a normal
result; do not record style preferences or possible improvements as bugs to
fill the list.

### When something goes wrong

Handle these yourself; none of them is a reason to stop or to ask the user.

- **The element is gone or the description is ambiguous.** The page changed.
  Call `observe` and pick the target from what is there now.
- **The page is still loading.** Call `observe` again, at most three times,
  then treat the slow load as the finding it is.
- **A call timed out or failed after a write** (submit, save, delete, pay).
  The write may have happened. Look first (`observe`, or `verify_persistence`
  on the page that would show it) and repeat the action only if it did not
  take effect.
- **The session failed or was closed.** Call `end_session` so the report and
  the journal of verified findings are written, then `start_session` again
  with the goals that are not finished. Carry the earlier findings into your
  final report.
- **A journey needs a login you do not have.** Mark that goal `blocked` with
  the reason, finish the other goals, and ask for the account in your report.
- **A check comes back inconclusive.** Follow its reason once (check the page
  that really shows the content, or a filtered view of a long list). If it is
  still inconclusive, report it as inconclusive. Never restate it as passed or
  failed.

## 4. Deliver

**Only if the user asked for a regression test file** (to keep in CI, for
example), export it first, while the session is open. `export_test` reads the
findings of the current session, and `end_session` closes it: after that the
call fails and the finding would have to be reproduced and recorded again in
a new session. It works for a finding verified by a fresh-load text check and
writes a Playwright test that fails while the bug is present. Pass `run=true`
only when the user authorized repeating the journey's writes (for example on
throwaway data), and report the TODO steps it lists rather than guessing them.
Application values are private parameters: only set
`save_private_values=true` when the user explicitly wants to retain local
replay values; the JSON file it returns may contain credentials and must
never be published or committed. Without such a request, skip this step:
exporting is not part of every run.

Then close with `end_session`, passing the final `coverage_updates` for the
goals you exercised or found blocked, with the evidence you observed, and tell
the user, in plain language:

- What you tested: the journeys, and the address.
- Each bug: what happens, who it affects and how badly, the steps to see it,
  and where the evidence is (the screenshot and report paths from Argus).
  Group by verdict: verified, observed (agent judgment), not reproduced,
  inconclusive. Never present an observed finding as verified.
- What was not finished and why: goals blocked, not reached, or cut by the
  budget.
- If nothing was found, say so and say what was exercised. Completing the run
  does not mean the application passed everything; it means these journeys
  showed no defect.
- The paths of any test files you exported.

Do not change the application's code unless the user asked for that. If fixing
is part of your task, fix, then re-test as in section 5.

## 5. Re-test after a fix

Argus keeps verified findings in a journal per site, so nobody has to
configure anything to check them again.

1. `start_session` on the same address with `review_mode="regression"`.
2. If the bug was a change that did not stay (a delete that came back, a save
   that was lost), the old data is still there: do that action once on the
   fixed build first.
3. Call `regression_check`. It re-runs each journaled finding's fresh-load
   check: STILL-PRESENT, NO-LONGER-REPRODUCES or INCONCLUSIVE. It is
   deterministic and read-only, so use it for known findings instead of
   exploring again.
4. For a finding that no longer reproduces, make sure the feature now works,
   not only that the old symptom is gone, and look at the flows next to the
   fix for anything it broke.
5. `end_session`, then report per finding: fixed, still present, or
   inconclusive with the reason. If the journal has no entry for the bug (it
   was observed but not verified, or recorded elsewhere), say so and re-test
   it by hand from the earlier report.
