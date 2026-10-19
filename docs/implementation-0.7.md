# Argus 0.7 implementation ledger

Goal: a modular local QA system with explicit resource ownership, safe evidence
exports and live observability. Existing MCP tool names and workflows remain
the compatibility baseline. No paid model evaluation is required for this work.

## Delivery checkpoints

- [x] Separate protocol entry points, application services, domain and adapters.
- [x] Introduce isolated runtimes, lifecycle states, cancellation and operation limits.
- [x] Remove private browser access from application services.
- [x] Share verification and regression between CLI and MCP.
- [x] Separate replay inputs from exportable evidence and secure capsules.
- [x] Validate configuration and classify operational failures.
- [x] Harden persistence, report exporters and active-run cleanup.
- [x] Add local monitoring HTTP endpoints, SSE, metrics and dashboard.
- [x] Add lint, types, architecture checks and fault/integration tests.
- [x] Document architecture, decisions, extension points and interview evidence.
- [x] Validate wheel and source distributions and run a wheel browser smoke test
  outside the source checkout.

## Baseline

Commit `4d2f2c5`, 264 tests passed on 2026-10-18. The scripted 34-scenario
benchmark is a tool capability check, not an autonomous-agent success rate.

Confirmed gaps: failed replacement leaves a closed session active; lifecycle
locking does not cover actions; replay input values reach JSON exports; CLI
imports MCP internals; monitoring is absent.

## Validation

Pure rules and lifecycle faults use fake backends. Browser integration uses
local fixtures. Protocol tests exercise stdio. Monitoring tests cover host and
origin validation, bounded event retention, reconnect gaps and shutdown.
Mandatory browser tests must fail when their runtime is unavailable in CI.
Release installation is tested outside the source checkout.

The scripted local benchmark retained 34/34 seeded findings and resisted all
3/3 false-positive baits. No paid model benchmark was run. Dashboard rendering
was checked in Chromium with no JavaScript errors. Ruff formatting/lint and
the eight-file core mypy scope passed. Test counts and supported-Python results
are recorded by CI rather than duplicated as a permanently current claim here.

## Delivery gates

The branch must pass CI before merging. The `v0.7.0` GitHub release workflow
re-runs tests, validates metadata and the installed wheel, then publishes PyPI
and MCP Registry metadata in dependency order. Source of truth for publication:
[release](https://github.com/chriswu727/argus/releases/tag/v0.7.0) and
[workflow runs](https://github.com/chriswu727/argus/actions/workflows/release.yml).

## Deliberate non-goals

No distributed scheduler, multi-tenant authentication, encrypted capsules or
whole-repository strict typing. Native macOS live UI was not exercised in this
web-focused release validation. See `architecture.md` for limits and failure
semantics; these boundaries must not be overstated in résumé claims.
