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

Count deltas require both measurements on the same URL. Invalid booleans,
fractional count deltas, empty text markers and malformed predicates produce
unchecked results rather than guesses.

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
