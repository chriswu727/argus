# Agent result and progress protocol

Availability: development on `main`; not included in PyPI 0.7.1.

Public tool names and arguments are unchanged. MCP responses retain text and
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
- `verify_persistence`: expected/present/matches and storage-reset policy.
  `finding_confirmed` is false: this probe does not independently record a bug.
- `record_bug`: recorded/duplicate outcome, finding reference, verdict, receipt.
- `record_observation`: recorded/duplicate outcome and qualitative evidence
  reference, never promoted to a certified finding.
- `coverage_update`: the evidence-linked coverage ledger; exercised is not
  synonymous with passing. A failed application journey can be exercised.
- Screenshot tools: image artifact paths plus unchanged MCP image blocks.
- `end_session`: original run ID, report path, warnings, coverage and counts.
  A null report path or nonempty warnings must not be treated as clean export.

Common credential patterns are redacted in the added structured data. Existing
text retains each tool's disclosure policy, including explicitly enabled sensitive
reads. Screenshots and arbitrary application prose may still contain private data.
No payload is permission to expand the operator's configured access policy.

## Progress transport

When the caller supplies an MCP progress token, Argus sends standard progress
notifications: operation started, browser launching, target loading, observation,
verification, reproduction outcome, coverage update, export and completion/failure
as applicable. A monotonically increasing ordinal is sent with **no total**;
it is not a percentage or an estimated time remaining.

Only fixed phase messages are allowed. Page contents, URLs, targets, input
values and model reasoning never enter these events. The loopback monitor SSE
receives the same phase IDs through the existing bounded runtime event stream.

The progress sink is request-local. Each notification has a 250 ms deadline;
after a delivery failure, further notifications for that request are skipped.
The final result still contains the captured stages. Cancellation is propagated,
backends are closed, and the operation lock is released. Notification delivery
is best effort and does not authorize retries of application writes.

Real stdio tests verify notifications and output-schema validation with the
Python MCP SDK. Host UI rendering differs: no promise is made that every host
displays notifications as a visible stream. The final envelope is the fallback.
