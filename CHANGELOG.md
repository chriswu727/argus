# Changelog

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
