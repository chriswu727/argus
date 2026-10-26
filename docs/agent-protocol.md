# Agent result and progress protocol

Available in 0.8.0 and newer.

Existing tool names and scalar arguments remain compatible; `coverage_update.goal`
also accepts a list of related goals. MCP responses retain text and
image blocks and add `structuredContent` validated against an advertised output
schema. Direct Python compatibility calls still return their original strings
or image-artifact lists. Structured results are constructed from application
data, never by parsing the rendered text.

## Envelope version 1

| Field | Meaning |
| --- | --- |
| `schema_version` | Result contract version, currently `1` |
| `operation` | Public tool name |
| `status` | `completed` or `error`; not an application-test verdict |
| `session_state`, `run_id` | Runtime state captured before releasing the operation lock; run ID may be null |
| `error_code`, `recovery` | Classified runtime error and conservative recovery guidance |
| `data` | Operation-specific evidence, or `{}` for tools not yet migrated |
| `stages` | Actual phases reached, in order; at most 64 |
| `progress_omitted` | Number of phase notifications beyond that bound |
| `progress_delivery_failed` | Progress delivery failed or exceeded its deadline; tool execution continued |

`isError` remains the MCP error signal. Runtime error text retains the legacy
`[error_code]` prefix. SDK-level argument/schema validation and unknown-tool
errors remain standard MCP errors and may not contain this envelope.

Never interpret `status: completed` as "the application passed". A persistence
probe can complete successfully with `data.verification.matches: false`.
Similarly, completing `record_bug` does not certify the finding: inspect
`data.verdict` and its receipt. `verified` means the specified symptom reproduced,
not that a root cause or defect interpretation has been proven.

## Structured evidence currently available

- `start_session`, web `observe`: page identity, description-keyed targets,
  counts, same-page count changes, feedback, coverage and omission counts.
  Structured targets are capped at 80, counts at 50, feedback at 10. Detailed
  text remains in the compatibility content; use `inspect_element` or
  `screenshot` when that observation is insufficient. These bounds do not claim
  to cap the entire legacy text response or browser memory.
- `test_action`: structural changes (at most 100), machine-checkable expectation
  results, screenshot path and auto-captured event count. A mismatch is a lead,
  not an independently confirmed finding.
- `click_what`, `type_into`, `select_into`, `press_key`, `navigate`, `go_back`:
  action execution metadata and post-action observation. `application_passed`
  is null: successfully executing an interaction is not a passing journey.
- `test_form`: field outcomes, partial-completion flag, post-submit observation,
  changes and screenshot reference. A failed field is not hidden by a successful submit.
- `verify_persistence`: expected/present/matches, storage-reset policy and the
  post-check page observation, so a separate `observe` is usually unnecessary.
  `finding_confirmed` is false: this probe does not independently record a bug.
- `record_bug`: recorded/duplicate outcome, finding reference, verdict, receipt.
  `finding_id` is session-scoped; duplicates link through `duplicate_of`.
  `evidence_status: observed` distinguishes an un-rechecked report from an
  attempted but inconclusive verification.
- `record_observation`: recorded/duplicate outcome and qualitative evidence
  reference, never promoted to a certified finding.
- `coverage_update`: the evidence-linked coverage ledger; exercised is not
  synonymous with passing. A failed application journey can be exercised.
  A list of 1-20 related goals updates atomically after all matches validate;
  duplicates/ambiguous/unknown goals reject the entire batch. Each goal retains
  its own evidence window. Batch only goals actually exercised by that journey.
- Screenshot tools: image artifact paths plus unchanged MCP image blocks.
- `end_session`: original run ID, report path, warnings, coverage and counts.
  A null report path or nonempty warnings must not be treated as clean export.

Common credential patterns are redacted in the added structured data. Existing
text retains each tool's disclosure policy, including explicitly enabled sensitive
reads. Screenshots and arbitrary application prose may still contain private data.
No payload is permission to expand the operator's configured access policy.

## Bounded verification recovery (unreleased)

`verify_persistence` reports whether the expected state held before reload and
whether the loaded page is conclusive (not an HTTP error or login wall). For an
observed match-to-mismatch transition it supplies `suggested_verify`: the inverse
text predicate at the checked URL, carrying the same storage policy. Review the
user requirement before passing it to `record_bug`. This is a fresh-load symptom
check, not an independent replay of the original journey or automatic proof of
a semantic bug. It does not repeat Save/Delete. Plain replay ends at the recorded
actions; it cannot stand in for an unrecorded persistence refresh.

To supplement an existing finding, call `record_bug` with its `finding_id`,
unchanged title/severity, a `verify` clause and no `evidence`. This returns
`updated: true`, `recorded: false`, the same ID, the new receipt and remaining
rechecks. It does not add a bug or advance the new-finding action cursor. A
finding allows at most two recheck attempts; already verified findings are not
rechecked. Earlier receipts remain in the exported `verification_history`.
Replay rechecks use the original finding's trace and still repeat its writes;
they require the same authority as any replay. A limit or inconclusive receipt
is a reason to preserve uncertainty, not to create a duplicate and force a pass.

## Guidance and compact output

The optional `agent` profile exposes 19 common browser-testing tools with concise
descriptions. Existing `core` (default), `screen` and `full` profiles are unchanged.
Use `--tool-profile agent --response-mode compact` for structured-result-aware
hosts. Action and verification results already carry page evidence: inspect it
before spending another call on `observe`.

Observations include at most four `next_checks`, with a fixed category, evidence
reason and suggested action. Suggestions use declared goals, page fields,
unvisited paths and interactions since the last persistence check. Three repeated
observations without new visible state or check evidence produce a stalled hint.
These are fallible applicability hints, not a test plan, permission grant,
proof of coverage or autonomous actions. The model must apply user constraints.

`--response-mode compact` (or `ARGUS_RESPONSE_MODE=compact`) replaces duplicated
prose with a short pointer to structured evidence for migrated operations.
The startup protocol and bounded visible text remain in structured data.
Unmigrated tools and errors retain their text; image blocks are unchanged.
`compatible` remains the default. A host must consume `structuredContent` to use
compact mode; measure actual host token usage rather than equating fewer bytes
with guaranteed savings. Direct Python calls keep their original text behavior.

In the unreleased compact/delta encoder, identical coverage evidence bundles
shared by two or more goals are returned once in `coverage.evidence`. Each such
goal has `evidence_ref: "e1"` instead of inline `evidence_refs`; resolve it in that
same coverage object's evidence map. IDs are local to the coverage snapshot, not
global or cross-response IDs. Unique bundles remain inline. Delta snapshots retain
or replace the entire coverage object, including its map. Compatible responses,
application state and exported reports retain inline evidence. No evidence is
discarded, and redaction precedes deduplication.

Count deltas require both measurements on the same URL. Invalid booleans,
fractional count deltas, empty text markers and malformed predicates produce
unchecked results rather than guesses.

## Incremental observations

Version 0.9 adds opt-in `--response-mode delta` (`ARGUS_RESPONSE_MODE=delta`).
It has compact text behavior and keeps the version-1 result envelope. Only
observation fields can be inherited: `page`, `visible_text`, `targets`, `counts`,
`feedback`, `coverage`, `omitted`, and `follow_up`. The observation lives directly
in `data` or in `data.observation`, depending on the operation.

- `snapshot: {id, mode: "full"}` replaces the client's observation base.
- `snapshot: {id, mode: "delta", base_id, removed}` requires the exact prior
  base ID. Inherit only the fields listed above, remove keys listed in `removed`,
  and replace fields returned in the new observation. Arrays and objects are
  replaced wholesale, not recursively merged. Empty values are meaningful.
- Action results, count changes, next checks, verification outcomes and all
  other fields are **current-call-only**; never inherit them from an old result.
- Without the exact base (including after context loss or interrupted delivery),
  call `observe` to get full state before reasoning or acting. Snapshot IDs are
  per server process, not persistent evidence IDs or an artifact download API.

`observe`, navigation, session start, and persistence reloads always return full
observations. A changed run or URL also forces a full snapshot. Errors, cancellation,
session end, oversized observations, and operations without a page observation
discard the retained base. After 16 consecutive deltas a full snapshot is sent.
Only one redacted observation is retained, bounded to 256 KiB of compact JSON;
oversized observations use the original compact representation without a snapshot.
The encoder uses a full snapshot when a delta would be larger. A full snapshot
adds small metadata overhead compared with compact mode.

Screenshot requests still return image blocks; paths to screenshots and reports
remain evidence references, not proof that their contents were inspected. Direct
Python application calls, compatible mode and compact mode do not use snapshots.
The decoder must not infer "unchanged" when a required base is unavailable.

## Overhead telemetry

The optional loopback monitor exposes `overhead` in `/api/status` and numeric
Prometheus counters in `/metrics`. Counts include result JSON bytes, text UTF-8
bytes, structured JSON bytes, image base64 bytes and image count. Categories
overlap; do not sum them to derive total bytes. JSON-RPC framing and transport
notifications are not included, and bytes are not model tokens or billed cost.

Request-local timing measures instrumented browser waits, state extraction,
screenshots, report export and response encoding. Timings include failed work
when the request returns a result; cancelled requests have no final response
sample. They are inclusive wall-clock components, not a complete latency partition.
No page text, URLs, arguments, input values or artifact paths enter these counters.
Snapshot evidence remains in process memory, not in the numeric monitor.

## Progress transport

When the caller supplies an MCP progress token, Argus sends standard progress
notifications: operation started, browser launching, target loading, observation,
verification, reproduction outcome, coverage update, export and completion/failure
as applicable. A monotonically increasing ordinal is sent with **no total**;
it is not a percentage or an estimated time remaining.

Only fixed phase messages are allowed. Page contents, URLs, targets, input
values and model reasoning never enter these events. The loopback monitor SSE
receives the same phase IDs through the existing bounded runtime event stream.
MCP messages may append numeric goal/check/pending-finding counts. The dashboard
shows these counts and active goal numbers, never goal descriptions or free-text
blocking reasons. Exercised goals are not necessarily passing goals.

The progress sink is request-local. Each notification has a 250 ms deadline;
after a delivery failure, further notifications for that request are skipped.
The final result still contains the captured stages. Cancellation is propagated,
backends are closed, and the operation lock is released. Notification delivery
is best effort and does not authorize retries of application writes.

Real stdio tests verify notifications and output-schema validation with the
Python MCP SDK. Host UI rendering differs: no promise is made that every host
displays notifications as a visible stream. The final envelope is the fallback.
In the 0.8 Codex CLI evaluation, JSON events contained completed tool results but
no separate progress-notification events. Use the local dashboard for a reliable
operator view in that configuration; do not assume CLI evidence proves UI streaming.
