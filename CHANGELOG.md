# Changelog

## Unreleased

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
