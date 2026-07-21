# Changelog

## Unreleased

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
