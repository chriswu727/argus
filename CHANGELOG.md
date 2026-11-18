# Changelog

## Plugin updates - 2026-11-12 (0.13.1)

- Claude plugin 0.13.1 and Codex plugin 0.13.1 pin the published Argus 0.13.1
  runtime and refresh their dependency locks and setup commands.

## 0.13.1 - 2026-11-12

- The first session downloads the Chromium build it needs instead of failing
  with an install command, so connecting Argus is the only setup step. A
  download that outlasts the call keeps running and `start_session` asks for a
  retry. `ARGUS_AUTO_INSTALL_BROWSER=0` restores the old behaviour.

## Plugin updates - 2026-11-12

- Claude plugin 0.13.0 and Codex plugin 0.13.0 pin the published Argus 0.13.0
  runtime and refresh their dependency locks and setup commands. The two
  plugins now share the runtime's version number.

## Plugin updates - 2026-11-10

- Claude plugin 0.12.4 and Codex plugin 0.12.3 pin the published Argus 0.12.3
  runtime and refresh their dependency locks and setup commands.

## 0.13.0 - 2026-11-12

One set of verdict rules. Live verification, `verify_persistence` and exported
tests decided "is the symptom present?" with three separate implementations
that had drifted apart; they now run the same code. No new tools.

Upgrading: regenerate exported tests to get the shared rules (earlier files
keep working with their old rules). "Present" now means text a user can see or
the value of a visible field, so text reachable only through `aria-label`,
unselected `<option>` text and closed `<details>` content no longer count.


- Description-driven web tools (`click_what`, `type_into`, `test_action`,
  `inspect_element` and the rest) resolve the description against the page as
  it is when the action runs, not against the last observation: a row that
  re-rendered in place is found by its current text, and a description that has
  become ambiguous or no longer matches returns candidates or a no-match error
  instead of acting.
- Screen-mode tools report failures and refusals as errors with a code
  (`permission_denied` for safety refusals and missing macOS grants, `timeout`,
  `invalid_argument`, `backend_error`) instead of completed calls.
- A `<textarea>` is named by its aria-label, `<label>`, placeholder or name, and
  its initial content is reported only as its value, so a secret-named field's
  content is hidden as `(value hidden)` instead of shown as its label.
- Row text separates adjacent elements that the markup writes without
  whitespace, as frameworks usually do. `<span>Buy groceries</span><button>Delete</button>`
  read as "Buy groceriesDelete", so "Delete near Buy groceries" matched nothing.
- A row's text (the `near:` context, repeating item lists and exported tests'
  row matching) leaves out `<textarea>` content, so a secret in a textarea no
  longer shows through the row of every control beside it. Dialog previews,
  toasts and the focused element follow the same rule, and the focused
  element's value is no longer collected.
- Fresh-load verification and `verify_persistence` read the same visible text:
  the page and every visible frame (cross-origin frames and open shadow roots
  included) plus the values of visible fields. Text that is only inside an
  iframe is now found, and hidden text inside a visible list row or element no
  longer counts as present.
- Login-wall phrases are searched in the main frame's visible text, so a sign-in
  banner rendered in a shadow root makes an absent symptom inconclusive.
- `verify_persistence` suspends the session's network mocks for its fresh load
  and restores them afterwards, so it can no longer confirm a response the agent
  injected, and reports an absent target on a page whose network never settles
  as inconclusive.
- Exported regression tests embed Argus's verdict rules and visible-text
  collector verbatim (the docstring names the rules version), so they decide a
  symptom exactly as fresh-load verification and `verify_persistence` do:
  unspaced scripts such as CJK match, hidden field values do not count, open
  shadow roots are read, and a redirect elsewhere or a non-English login form
  makes an absent symptom inconclusive.
- `export_test(run=True)` reports a new outcome, `inconclusive`, with a reason
  code, for an error page, login wall, redirect, unsettled or unreadable page;
  these were `errored` before. The file's last stderr line starts with
  `ARGUS_INCONCLUSIVE`; the test fails under pytest and the file exits 3 when
  run as a script. Earlier exports keep their old rules until regenerated.
- A child frame that cannot be read now gives its own inconclusive reason
  instead of claiming the page text exceeded the inspection limit.
- Capsule liveness markers, `test_action`'s `text_present`/`text_absent` and the
  action diff read the same visible frame text.

## 0.12.3 - 2026-11-10

Fixes from a full audit of 0.12.2. Journals and exported tests stay compatible.
Some checks that returned `FIXED` or `verified` on evidence that could not show
the symptom now return inconclusive, and failed actions are reported as errors.
Screen mode is experimental.

- Check symptom text against the whole page instead of its first 5,000
  characters, so text further down is no longer reported as gone. Match targets
  inside Chinese, Japanese, Korean and Thai text. Keep lists that share a class
  or tag apart.
- Load a new document for fresh-load checks of hash-route URLs; a same-document
  navigation previously read in-memory state.
- Treat an HTTP-status check as inconclusive when the load answers with a
  different error, is redirected or lands on a login page. Only a clean answer
  from the requested page counts as no longer reproduced.
- Treat an absent symptom as inconclusive when the load ended on another page
  or shows a login form in any common UI language.
- `argus-regression` exits 2 for a URL without a scheme or with credentials and
  for journal entries of the wrong type, and names other origins that have
  journals when the given one has none.
- Report intermittent receipts as inconclusive in JUnit (a failure, labelled
  INTERMITTENT), SARIF and the HTML badge. Map SARIF `critical` to `error`.
  Strip characters XML cannot carry. Show inconclusive checks as INCONCLUSIVE in
  HTML. Redact regression artifacts.
- `argus-cleanup` ignores report files whose name is not a run ID; one such
  name could select the whole output directory. Automatic screenshot names can
  no longer leave the run directory.
- Make credential redaction linear in input size and cap captured request
  bodies. Stop redacting ordinary keys such as `author` and `sidebar`; redact
  `pwd`, quoted values, full Bearer and Basic values, signatures and private keys.
- Observe forms that contain a control named `id`. Attach page listeners once.
  Accept unsaved-changes prompts so navigation proceeds. Recover after a failed
  navigation. Judge internal links by origin.
- Act on the exact element that was listed. Each observed element keeps a
  reference to its DOM node, so a click or fill no longer lands on an earlier
  control that shares a label prefix, on a hidden hover-only control in another
  row, or on a same-named field in another form. An element that has been
  removed fails the action instead of redirecting it.
- `test_form` with automatic submit only considers controls in the form that
  was filled.
- Screen mode: send keyboard and coordinate input only while the bound app is
  frontmost; a session started without a target stays with its first app.
  The abort file now also stops `screen_launch` and `screen_quit`, and a new
  session no longer deletes it: only the user clears it. `screen_quit` needs an
  exact or unique app match. Hold, drag and click counts are bounded, key
  combinations apply modifiers per key and accept hyphenated key names, and
  failed typing no longer echoes the text. `screen_wait_for_stable` works.
  `ARGUS_SCREEN_ABORT_FILE` expands `~`, and its directory is created at start.
- `test_action` and `test_form` return the console and HTTP failures an action
  raised, and the cross-stack check, as structured data (`browser_events`,
  `cross_stack`); compact and delta responses previously dropped them.
- Failed actions (hover, resize, upload, drag, coordinate input, missing
  screenshots, a screen session that cannot start) are reported as errors
  instead of `status: completed`.
- `argus-regression` gives each finding its own deadline; a long journal or one
  unreachable page no longer fails the whole run.
- Coverage goals match on whole words.
- Network mocks are registered on the browser context with one handler per
  pattern. Replacing a mock no longer leaves the first response active during
  a fresh-load verification, and mocks follow tab switches and device emulation.
- CI also runs on Python 3.13 and 3.14.
- Screen mode reads the frontmost app from the accessibility and window
  servers. `NSWorkspace` is only refreshed by a running main run loop, so a
  long-lived server kept seeing the app that was frontmost at startup and
  refused all input. `screen_quit` and `screen_launch` report a name that
  matches several running apps instead of "not running".
- A start rejected after launch closes the browser; `end_session` always leaves
  the failed state. `test_form` no longer echoes a typed value when a fill fails.
- Resolve "Last name" and "First name" to the fields with those labels instead
  of reading the word as a position. Only the last kind word sets the kind, so
  "Select all button" and "Menu button" pick the button. A bare label shared by
  several rows stays ambiguous. Chinese, Japanese, Korean and Thai descriptions
  match inside longer labels.
- List elements in `srcdoc` and blank iframes once, and reach controls in
  cross-origin frames nested inside other cross-origin frames.
- An exported step located by a structural CSS path (`:nth-of-type`,
  `:nth-child`) is positional: the export is incomplete and does not run.
- `drag_what`, `right_click`, `storage_set`, `storage_remove`, `storage_clear`,
  `cookies_set`, `cookies_clear`, `tabs_switch` and `eval_js` appear in the
  action trace without their values, so an export that contains them is
  incomplete and a replay through them reports that it cannot repeat them.
- `crawl_site` stays on the origin it started from, skips a link that redirects
  elsewhere, returns to the start page, and counts only the event-bugs it captured.
- `network_mock`, `network_unmock`, `network_clear_mocks`, `tabs_close` and
  `set_dialog_handler` appear in the action trace, the mock without its body and
  the dialog handler without its text, so an export after a mock is incomplete.
- A recheck that verifies a finding recorded without a goal updates the coverage
  evidence of every goal whose window holds it, not only of tagged goals.
- Element descriptions hide the value of a text field whose name, id, label or
  placeholder names a secret, such as `api_key`.
- `wait_for_request` redacts credentials in the matched URL.
- The monitor rejects same-site requests, such as an image on the app under test
  pointing at the event stream; only same-origin, direct and non-browser
  requests are accepted.
- An empty `ARGUS_TOOL_PROFILE` means `web`, and an invalid one is reported as a
  usage error naming the valid profiles instead of a traceback.

## Documentation and Glama deployment - 2026-11-09

- Synchronize the agent installation guide and protocol with Argus 0.12.2,
  including journal migration, regression verdicts and export preconditions.
- Publish Glama 0.12.2 from release commit
  `e2ac33da3b3eda998c3ba98084c33ed291381b66` after adding `libatomic1` to its
  build steps. The verified build reports MCP server version 0.12.2.
- Document the system dependency and the wait between build success and image
  availability for release creation. The runtime package version is unchanged.

## Plugin updates - 2026-11-08

- Claude plugin 0.12.3 and Codex plugin 0.12.2 pin the published Argus 0.12.2
  runtime and refresh their dependency locks and setup commands.
- Regenerate older exported tests to use the regression reliability fixes;
  keep legacy journals and re-record findings in a new report directory.

## 0.12.2 - 2026-11-08

- Treat intermittent regression receipts as inconclusive instead of fixed.
- Retain full verification predicates and preconditions privately for journals
  and exports; public receipt text remains a preview. Hash complete predicates
  for deduplication, including URL queries and fragments.
- Require re-recording legacy journal entries whose verification conditions are
  incomplete. Storage-reset checks remain excluded from automatic regression
  and test export rather than silently dropping the reset.
- Detect unexpected password login forms while allowing recorded checks of a
  login page itself. Stop ambiguous row selection before replaying its action;
  positional fallbacks require manual translation.
- Bound exported network waits like live verification. Busy pages can still
  provide positive symptom evidence; unsettled absence stays inconclusive.

## Plugin updates - 2026-11-05

- Claude plugin 0.12.2 and Codex plugin 0.12.1 pin the published Argus 0.12.1
  runtime, refresh their dependency locks and document private replay values.
- Keep raw replay values out of shared artifacts; retention remains opt-in.

## 0.12.1 - 2026-11-05

- Keep all application URLs, inputs, locator strings and assertion text out of
  generated regression tests, including unknown query keys and path tokens. Read
  them from environment variables; expose names and step purposes in `required_env`.
  Omit finding prose from file contents and filenames. Preserve base-URL overrides.
- Add opt-in `save_private_values` for retaining a local, Git-ignored JSON values
  file with owner-only POSIX permissions. Standalone tests can load it through
  `ARGUS_VALUES_FILE`; explicit environment values take precedence. No values file
  is written by default, and values are never included in tool results. Export
  identities reject another test's file or environment before any browser actions.
- Withhold raw browser and subprocess error output from generated test failures
  and `export_test` results. Keep explicit symptom, incomplete, login-wall and
  missing-variable diagnostics. Missing input values block execution.
- Stop incomplete exports before browser actions instead of silently skipping
  unsupported steps. `export_test(run=True)` reports `incomplete` without running.
- Shorten the project overview, move detailed usage and tool profiles to
  `docs/usage.md`, and consolidate public documentation in English.

## Claude plugin 0.12.1 - 2026-10-28

- Use `uv run --isolated --locked` without launcher environment overrides.
  Each server gets a temporary environment populated from the existing lock;
  cached downloads are reused and normal exit removes the environment. The
  launcher neither modifies the plugin directory nor uses the host environment.
- Keep the server pinned to Argus 0.12.0 and preserve all dependency hashes.
  This is a Claude plugin update, not a new Python package or Codex plugin release.

## 0.12.0 - 2026-10-28

- Exported regression tests reject HTTP error pages and login walls instead of
  treating a missing symptom there as a passing check. Frame controls retain
  their full iframe chain, duplicate controls are disambiguated within that
  frame, and text checks include nested frame content.
- Fix the headless-shell-only doctor test finding the browser cache on macOS,
  and select the shell matching the installed Chromium revision.
- New `export_test(finding_id, run=False)` turns a finding verified by a
  fresh-load text check into a standalone Playwright test. The test replays the
  journey recorded up to the finding, using locators captured from the elements
  Argus resolved (scoped to their row when a control repeats), then asserts the
  opposite of the verified symptom: it fails while the bug is present and passes
  once it is fixed. Sensitive inputs are read from environment variables (a
  `run=true` from the session passes its values that way, never in the file), and
  steps it cannot translate are marked TODO and listed. `run=true` runs it once
  to show that it fails on the symptom. The `web` profile now has 29 tools
  (`advanced` 62, `full` 78).
- `hover_what` hovers the exact visible text when no interactive element
  matches, so a list row can be hovered by its title to reveal hover-only
  controls; replays and exported tests repeat it. The "no element matches"
  message notes that such controls appear after hovering their row. Found when
  a Sonnet run on TodoMVC could not hover a row by its text.
- `go_back`, `scroll_down` and `upload_file` are now recorded in the journey.
  Before, replay (`record_bug` with `replay`, `regression_check`) and
  `export_test` skipped them silently and could follow a different path. Replay
  now repeats going back, scrolling and `resize`, and reports an upload as not
  replayable instead of passing over it. Exported tests go back, scroll and set
  the viewport size, and mark an upload TODO. A replay now starts on the page
  where the recorded journey began, not at its first `navigate`, so going back
  follows the same history.
- Exported tests stay stable as the app changes, after Haiku runs on TodoMVC
  and BuggyTasks: a repeated control is scoped by the fewest leading words of
  its row (its title), not by changing details such as "5 days ago"; an unnamed
  checkbox or radio is found by its input type inside its row instead of by a
  DOM path; role locators use the element's explicit role; and helpers are
  written only when the test uses them.
- Interactions that real apps need, found by running the test skill on TodoMVC
  and Flask's tutorial app:
  - `hover_what` returns the observation taken while hovering, so controls that
    appear only on hover (a row's delete button) are listed and can be clicked.
  - Elements are located among visible matches only. A hidden duplicate (the
    same hover-only button in another row) no longer takes the target's place.
  - `click_what` and `test_action` accept `double_click`, which can also target
    exact visible text such as an inline-edit label. Replays repeat it.
  - A JS alert, confirm or prompt raised by a click is reported in the result
    instead of being dismissed silently; `accept_dialog=true` accepts that one
    dialog when the user authorized the action it confirms.
  - A form field with no id, name, placeholder or aria-label attribute, named by
    a nearby `<label>` (TodoMVC's inline editor), was listed but could not be
    typed into: its selector used the label text as an `aria-label` attribute.
    Such fields are now located by a structural CSS path.
- Both test skills now include a visual review step: take a `screenshot` of each
  key page, judge hierarchy, claims, field order and cross-page totals, run
  `check_layout`, and record such judgments with `record_observation` (a text
  check on a fresh load is still required before `record_bug`). On DarkShop a
  Haiku run with this step found 9 of the 12 seeded problems, up from 3, and
  kept the visual ones as observations.
- `observe`'s description, the "no element matches" message and the agent guide
  now say that a target description from `observe` can be passed as-is to
  `click_what`, `type_into`, `select_into` and `test_action`, instead of telling
  the agent to rephrase.
- `record_observation` keeps an observation whose `category` it does not
  recognize, recording it as `usability` with a warning that lists the known
  categories, instead of rejecting it. A Haiku run of the test skill lost an
  observation this way with `"security"`.
- `test_form` sets checkbox, radio and switch fields from `true`/`false` (or
  on/off-style text) instead of trying to type into them, which failed with
  "Input of type checkbox cannot be filled" and skipped the field.
- Text checks (`verify`, `verify_persistence`, `text_present`) accept a target
  that starts or ends with punctuation even when it touches other text, such as
  a "-50%" badge printed straight after "$89.99". Word boundaries still apply
  where the target begins or ends with a letter or digit.
- `record_bug` keeps a finding whose `bug_type` it does not recognize. It is
  recorded as `ux_issue` with an evidence warning that lists the known types,
  instead of rejecting the whole finding and forcing a resubmission. A small
  model running the test skill lost a call this way with `"inconsistency"`.
- When a description matches nothing, the "Closest on the page" shortlist now
  includes unlabeled controls such as row checkboxes, ranked by the row they sit
  in. Before, they never appeared there because only an element's own label was
  compared.
- Both test skills now ask the agent to use `test_action` with an `expect`
  prediction for clicks that should change state, so Argus reports MATCH or
  SURPRISE instead of leaving the comparison to the agent. In two Haiku runs of
  the skill, every state-changing click went through `click_what`.
- Fix `click_what` (and the other description-based tools) rejecting the target
  descriptions `observe` itself returns. State markers such as `[unchecked]`,
  `(near: …)` and `->` were scored as words, so a row checkbox or a row's Delete
  button copied verbatim from `observe` matched nothing. An observed description
  now resolves to its own element, row context is shown with collapsed
  whitespace, and an unlabeled control can be named by kind plus row
  ("checkbox for Buy groceries"). Found by running the `/argus-testing:test`
  skill with a small model, which could not complete or delete tasks.
- Add a `/argus-testing:test` skill to the Claude plugin and an `argus-test`
  skill to the Codex plugin. Each agrees the scope (asking before real-world
  effects), checks the environment, runs the session, rechecks symptoms on fresh
  loads, and reports findings by verdict with coverage and report paths; code
  changes and repeated writes need the user's request. The Claude skill installs
  Chromium from the plugin's own locked environment, so its browser matches the
  lock exactly.
- The Claude plugin keeps its virtual environment in `${CLAUDE_PLUGIN_DATA}/venv`
  instead of the installed plugin files, following Claude Code's guidance not to
  write state under the plugin root; uv keeps it in step with the lock.
- Prepare for MCP Python SDK 2.x without switching: every version-sensitive
  name (FastMCP/MCPServer, the server version, camelCase/snake_case result and
  tool fields) now goes through `argus.entrypoints.mcp.compat`. With mcp 2.3.0,
  `python -m argus.install_check` passes for every profile. A non-blocking CI
  job installs mcp 2.x over the 1.x pin and runs that check plus
  `tests/test_mcp_compat.py`. The release dependency stays `mcp<2`.
- Importing `argus.models`, `argus.detector` or `argus.reporter` now emits a
  `DeprecationWarning` naming the replacement in `argus.domain` or
  `argus.adapters.reporting`. They still re-export the same objects and are
  scheduled for removal in 0.13. Argus itself no longer imports them.
- Add a Codex plugin bundle (`plugins/codex`, Agent Plugins 1.0 format) served
  from this repository's marketplace: `codex plugin marketplace add
  chriswu727/argus`, then `codex plugin add argus-testing@argus`. It runs the
  same locked 0.11.0 runtime as the Claude bundle from the installed plugin
  root; tests keep both bundles' project and lock files byte-identical. The
  shared runtime project is renamed `argus-testing-plugin-runtime`, which only
  changes the lock's root entry.
- `argus-mcp --doctor` now checks web testing on every platform: a real
  headless Chromium launch decides readiness (so `--only-shell` installs pass,
  with headed Chromium reported as optional), a missing browser is reported
  for the installed Playwright version, and the report directory is writable
  (probed with a temporary file, since `os.access` always succeeds for root). Each failure prints the exact fix
  for the interpreter running Argus; screen-mode probes run only on macOS and no
  longer report `n/a` as passing elsewhere. Browser launch failures in
  `start_session` name the same install command, distinguish missing system
  libraries, and point to `--doctor`.
- Add a project-maintained Claude plugin bundle with an exact 0.11.0 package
  pin, a locked dependency environment, the existing brand icon, the default
  web tool profile, local-execution and privacy disclosures,
  and a no-browser/no-model stdio launch check. Directory approval is separate
  from publishing the bundle. The Python distribution is unchanged.
- Serve the bundle from this repository's marketplace, so it installs with
  `/plugin install argus-testing --marketplace chriswu727/argus`. Document a
  path-independent warm-up that pins Argus and the locked Playwright version and
  installs the matching Chromium; with a cold cache the first start otherwise
  downloads Python and every locked dependency before the MCP handshake. Other
  dependencies are resolved afresh, so the warm-up usually removes most of that
  download rather than guaranteeing an exact lock match. A test keeps the
  warm-up pins in step with the manifest and lock, and the launch check keeps
  proxy and certificate settings when it spawns uv.

## 0.11.0 - 2026-10-26

Compatibility: `--tool-profile agent` and `--tool-profile core` (and the same
`ARGUS_TOOL_PROFILE` values) still start the server, but they now load the new
`web` profile, so their tool sets change: `agent` grows from 19 to 28 tools and
`core` drops from 30 to 28 (`check_links` and `check_performance` move to
`advanced`). The full discovery payload (tool JSON plus server instructions) is
34,379 bytes for `web`, versus 24,386 for 0.10 `agent` (+41.0%) and, for 0.10
`core`, 51,732 on Python 3.11/3.12 (-33.5%) or 50,564 on 3.13 (-32.0%). These
are bytes, not model tokens. The `argus <url>` CLI
command and the `argus.bench` / `argus.evaluation` modules are removed from the
installed package.

- Lead with testing: "Argus — Testing for Coding Agents" helps coding agents
  test the apps they build, find bugs and verify them with evidence. Package
  name, commands, links and registry identity are unchanged. Describe Playwright
  MCP accurately, including its opt-in test assertions.
- Replace the `agent` and `core` profiles with one 28-tool `web` default that
  covers the whole testing loop, including `regression_check` and `get_errors`,
  with concise descriptions. Add `advanced` (61 tools) for network mocks,
  cookies and storage, tabs, dialogs, waits, coordinate input, crawling and
  link/performance probes. `screen` is unchanged and `full` still exposes all
  77 tools. Monitoring is unchanged and remains opt-in.
- Remove the standalone `argus <url>` CLI mode, its LiteLLM planner and the
  LiteLLM-driven bench runner. Argus is a tool for the host agent and never
  calls a model itself. `argus-regression` and `argus-cleanup` are unchanged.
- Drop LiteLLM and PyYAML, plus the Jinja2, aiohttp, multidict and urllib3
  security floors that only applied to LiteLLM's dependency tree; none is
  installed at runtime any more. PyJWT, cryptography and certifi floors stay
  because MCP still installs them. A fresh runtime install resolves 41
  packages instead of 82, and pip-audit (OSV) reports no known vulnerabilities.
- Move benchmarks and evaluations to the repository-only `argus_lab` package.
  They ship in the source archive but not the wheel; run them as
  `python -m argus_lab.bench` and `python -m argus_lab.evaluation` from a checkout.
- `verify_persistence` treats observed presence as conclusive and only absence
  on an HTTP error or login page as inconclusive (0.10 treated every such page
  as inconclusive). A new `expect_status` makes an intended status, such as 404
  after a delete, conclusive. Checks record the HTTP `status`, the expected
  status, and whether the text and the status each matched, also in exports.
- Re-finalizing a goal extends its evidence window while no other goal has
  started or been finalized, so updated evidence text no longer points at an
  earlier window. Windows closed by other work stay closed.
- Fresh-load finding screenshots start from the page top after an unsuccessful
  scroll scan. Redirected checks keep the checked URL and warn which page the
  screenshot shows.
- Recheck receipts carry `recheck` and `previous_verdict`, shown in reports;
  history records each clause. Interrupted rechecks keep an explicit
  inconclusive entry instead of an empty one.
- Action diffs no longer report field-value changes when rows shift position,
  and report generic page-text changes only when nothing more specific changed.
- Move the report service to `argus.adapters.reporting.reporter` and migrate
  internal and test imports to `argus.domain` and `argus.adapters`.
  `argus.models`, `argus.detector` and `argus.reporter` remain as
  compatibility re-exports, guarded by a test against new internal use.
- Normalize docstring-based tool descriptions with `inspect.cleandoc`, so
  hosts receive identical descriptions on Python 3.11, 3.12 and 3.13 (3.13
  dedents docstrings at compile time; earlier versions kept the indentation).
  The overhead benchmark now records the Python version.
- Advertised input schemas degrade to the SDK default if FastMCP's internal tool
  manager changes; a test covers every profile.
- Fold the 0.7 implementation ledger's lasting validation rules and non-goals
  into the architecture guide; the ledger remains in git history. Mark the 0.8
  evaluation as a historical result under its stated conditions. Regenerate the
  deterministic encoding result for 0.11.0; encoding sizes are unchanged and
  only discovery changed with the new profiles.

## 0.10.0 - 2026-10-26

- Synchronize the English README, protocol, architecture, installation and
  privacy guidance; add a dedicated Simplified Chinese README while keeping
  English primary. Refresh the deterministic encoding result for this version.
- Capture fresh-load finding screenshots before returning to the caller's page,
  and default the finding URL to the checked page. Preserve explicit URLs and
  screenshot opt-outs; report unavailable or mismatched evidence without
  changing the verification verdict or repeating application writes.
- Freeze goal evidence windows when switching testing groups. Record revisited
  action URLs and explicitly associate deferred findings with declared goals,
  including already finalized goals, without completing them automatically.
- Declare finding evidence fields in MCP discovery. Preserve expected/actual
  outcomes across report formats and retain caller-supplied call numbers as
  claimed references, never independent verification.
- Accept explicit final coverage batches in end_session, validating the entire
  batch before shutdown and preserving each goal's evidence window. Reuse the
  same coverage rules for mid-session and final updates.
- Remove redundant generated input-schema titles while preserving parameter
  names, constraints, defaults, validation and tool display titles. Omit empty
  optional success metadata in compact/delta envelopes using version-1 defaults.
- Update the no-model smoke workflow to reuse post-action observations and
  finalize coverage during close, reducing its Argus path from seven calls to
  five without dropping the save/reload check or exported coverage.
- Guide observed persistence failures toward fresh-load symptom checks instead
  of replaying writes. Keep login/error-page results inconclusive and preserve
  the distinction between a suggested check and a verified finding.
- Allow up to two explicit rechecks of an existing finding by session ID,
  preserving receipt history, deduplication, original replay steps and capsule
  expiry guards. Already verified findings do not consume another recheck.
- Share identical coverage evidence bundles in compact/delta MCP output while
  preserving compatible responses and full report evidence.
- Detect page-text and field-value changes in action summaries without adding
  raw input values to the diff.

## 0.9.1 - 2026-10-25

- Publish a distribution-alignment patch following a stale-commit Glama build
  associated with its 0.9.0 release. Runtime behavior is unchanged from 0.9.0.
- Document manually gated directory releases: synchronize the repository and
  verify the source commit and MCP handshake before publishing.

## 0.9.0 - 2026-10-25

- Report the Argus product version in the MCP handshake instead of the SDK
  version. Add a no-browser installed-package stdio check and run it against
  fresh release wheels outside the source checkout.
- Remove redundant output-schema display titles without changing validation
  constraints or tool display titles.
- Add opt-in delta observations with explicit base IDs, field removals, full
  resynchronization, lifecycle invalidation and bounded redacted retention.
  Preserve errors, current-call verdicts, image blocks and compatibility defaults.
- Measure response bytes and instrumented browser waits, observation, screenshots,
  report export and encoding. Expose numeric totals through loopback status and
  Prometheus; keep page content and input values out of monitoring.
- Add reproducible, lossless encoding comparisons with navigation controls and
  gateway discovery/response measurements. Do not equate byte savings with model
  token savings or claim a new model effectiveness evaluation.

## 0.8.0 - 2026-10-25

- Add an optional 19-tool agent profile with concise discovery descriptions,
  compact structured responses, post-action/check observations and atomic batch
  coverage updates. Keep core as the backward-compatible default.
- Add bounded next-check guidance, persistence reminders and stalled-exploration
  signals. Do not automatically mark goals complete or findings confirmed.
- Treat missing counts and malformed expectations as unknown, not guessed
  matches. Preserve partial form outcomes and stable finding references.
- Show real testing phases, goal/check counts and separate finding outcomes in
  the read-only loopback dashboard, without exposing goal text or inputs.
- Add opt-in, budget-bounded Codex subscription collection, synthetic browser
  fixtures, conventional-browser controls and independent evidence adjudication.
  Model calls are never part of import, normal MCP usage or CI.

- Add versioned structured MCP results alongside compatible text/image content,
  including structured evidence for core observation, coverage, verification, findings
  and report flows, with explicit runtime error codes and recovery guidance.
- Emit bounded, request-local phase progress over MCP and local monitoring;
  retain a final summary when a host does not display notifications.
- Add paired evaluation schemas and offline metrics with matched controls,
  retained failures, duplicate/false-positive accounting and unknown usage.
  A no-model browser/stdio smoke suite exercises buggy, fixed and clean controls.

## 0.7.1 - 2026-10-25

- Require Python 3.11 or newer and raise Pillow/filelock/certifi minimum versions to
  patched releases. Upgrade the Python interpreter before upgrading Argus.
- Restrict source distributions to explicitly selected product files and validate
  built archive contents before publication. Use private vulnerability reporting
  for security disclosures and omit personal contact details from package metadata.

- Parse structured credentials before redaction, omit multipart bodies, and
  redact captured request URLs and custom credential-bearing headers.
- Apply one redaction policy to all report formats without altering live state
  or executable journal conditions. Screenshots still require manual review.
- Compare full normalized origins before restoring capsule storage.
- Require approved local directories for file inputs and evidence imports;
  hide cookie/storage values unless enabled at startup. Add optional tool-target
  origin restrictions with explicit network-isolation limitations.
- Restrict Docker copy inputs, exclude local secrets and capsules, raise security
  dependency minimums, audit dependencies in CI/releases, and pin Actions and the
  checksum-verified MCP publisher. Add adversarial security regression tests.

## 0.7.0 - 2026-10-19

- Split MCP adaptation from focused application services, domain contracts and
  adapters; add explicit runtime composition and injectable browser, journal,
  verification and report extension points.
- Serialize all session operations; classify operational errors, validate
  runtime/capture limits, and clean up on failed startup, cancellation, timeout
  and shutdown. Do not automatically retry browser writes.
- Add optional loopback monitoring: dashboard, liveness/readiness, versioned
  status, bounded SSE with reconnect gaps, and Prometheus metrics. Keep all
  monitoring payloads free of tool arguments and page contents.
- Keep input values in a session-local vault and omit them from persisted
  action steps/replay exports, including bulk form filling and paste paths.
- Share CLI/MCP regression through application services and a verifier registry;
  use exit 3 for inconclusive checks and exit 2 for corrupted journals.
- Replace stale-file lock deletion with OS-backed file locking; coordinate
  report generation and cleanup and split individual report exporters.
- Add real stdio MCP and HTTP-monitor integration tests, runtime fault tests,
  architecture boundaries, Ruff checks and scoped core type checking in CI.
- Add architecture, design decision and extension documentation. Existing
  public MCP tool names and profiles are retained.

### Also included since 0.6

- Bound browser network, error, download, and dialog capture; avoid loading
  binary or oversized response bodies and redact credentials at capture time.
- Give every run a unique artifact identity so screenshots and reports cannot
  overwrite evidence from another session.
- Add atomic, schema-versioned journal and capsule persistence with visible
  diagnostics instead of silent write failures.
- Add the dry-run-first `argus-cleanup` command for removing complete old report
  runs without touching regression journals or state capsules.
- Serialize session lifecycle transitions and surface teardown, capture-limit,
  and secondary-report warnings in the final session summary.
- Keep the MCP SDK on its supported 1.x API and fail fast on package-import
  incompatibilities before browser installation in CI and release builds.
- Link each completed or blocked coverage goal to the URLs, value-redacted
  actions, screenshots, persistence checks, bugs, and observations produced
  during its testing window.
- Render structured goal evidence in HTML reports and preserve the same stable
  shape in JSON reports.

## 0.6.0 - 2026-07-21

- Move the full QA protocol from the global MCP instruction into the
  one-time `start_session` response, with a regression budget for the default
  tool context.
- Add natural-language testing goals, user constraints, and an advisory time
  budget to `start_session`.
- Add `coverage_update` with evidence-required `exercised` and `blocked`
  states; keep goal and discovered-page coverage visible during observations.
- Preserve completed, blocked, and unfinished coverage in HTML and JSON
  reports and in the final session summary.

## 0.5.0 - 2026-07-14

Argus 0.5 replaces the index-keyed MCP interface with a smaller,
description-keyed workflow and makes evidence preservation part of the
public contract.

- Add focused `core`, `screen`, and `full` MCP tool profiles.
- Replace integer-index actions with `observe`, `click_what`, `type_into`,
  `select_into`, and description-keyed inspection tools.
- Add independently verified reproduction receipts for text and HTTP-status
  findings, including cold replay for multi-step journeys.
- Preserve complete evidence in JSON, HTML, JUnit, and SARIF reports while
  keeping HTML screenshot previews compact.
- Correlate console and network symptoms without duplicating root causes.
- Add native macOS screen-mode permission checks, action limits, abort control,
  and before/after evidence capture.
- Improve visual inspection with element crops, screenshot diffs, bounded
  layout checks, and finite CSS-transition waits.
- Add packaging, CI, PyPI Trusted Publishing, and official MCP Registry
  metadata for repeatable releases.

### Breaking changes from 0.4

- `get_page_state` is now `observe`.
- `click`, `type_text`, and `select_option` are replaced by description-keyed
  actions.
- `verify_action` is now `verify_persistence`.
- `test_crud` is removed; compose journeys from the focused tools instead.
- Findings must be explicitly recorded with `record_bug` or
  `record_observation`.
