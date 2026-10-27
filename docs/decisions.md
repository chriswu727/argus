# Design decisions

## ADR 1 — A modular monolith with explicit runtime ownership

Status: accepted for 0.7.

The product is launched locally by an MCP host and controls one interactive
session. Splitting it into network services would add deployment and consistency
cost without an established multi-tenant requirement. Keep one process with
protocol/application/domain/adapter boundaries and injected dependencies.

Trade-off: the browser still shares a failure domain with the process. If hosted
multi-user execution becomes a real requirement, move each runtime into an
isolated worker, add identity/authorization, durable jobs and resource quotas;
do not expose the current monitor as a hosted API.

## ADR 2 — Serialize browser state; do not silently retry writes

Status: accepted for 0.7.

DOM caches, navigation and evidence cursors are stateful. A single operation lock
gives a clear ordering between actions and lifecycle changes. Bounded queue and
execution waits provide explicit backpressure. Cancellation closes the backend
instead of continuing from an uncertain UI state.

Trade-off: a slow action delays subsequent tools. Multi-session throughput uses
separate runtimes/processes, not parallel mutations of the same browser. A timed
out click might already have reached the application: the caller must inspect
before retrying, and Argus cannot promise exactly-once external side effects.

## ADR 3 — Atomic local files and OS-backed locks

Status: accepted for 0.7.

Reports are user-owned artifacts, and the journal is a small per-origin index.
Keep inspectable versioned JSON and atomic replacement, with repository-owned
merge locks. Avoid time-based "stale lock" deletion: a slow valid writer must not
lose its lock, and removing a locked inode can split the lock domain.

Trade-off: this is not an ACID database, and individual artifacts are not a
transactional set. Migrate the `JournalPort` to SQLite when query patterns,
volume or atomic multi-record requirements justify it. Use a server database
only after a hosted multi-user design is actually needed.

## ADR 4 — Bounded SSE for a read-only local monitor

Status: accepted for 0.7.

The dashboard needs server-to-client status updates, not bidirectional control.
SSE offers browser reconnect and sequence IDs without another protocol. A shared
ring prevents slow clients from building unbounded private queues. A gap is
explicit and the client refreshes the authoritative snapshot.

Trade-off: events disappear on process restart and after retention expires.
This is operational visibility, not a durable audit log. Authentication/TLS,
remote roles and distributed traces are out of scope for a loopback tool.

## ADR 5 — Separate executable inputs from exportable evidence

Status: accepted for 0.7.

Reliable replay sometimes needs sensitive input; portable reports do not need
those values. Store them in a per-session vault and export required-input
markers. Never treat a redacted placeholder as valid input. Capsules are an
explicit, credential-containing exception, not ordinary report data.

Trade-off: exported replay is intentionally not self-contained. A future secure
import workflow must require new input binding and explicit approval before
repeating writes. Memory and screenshots are not secure secret stores.

## ADR 6 — Keep extension seams narrow

Status: accepted for 0.7.

Register verification strategies and pure report renderers; inject browser and
journal implementations at the composition root. Use small Python protocols,
not a framework-wide dependency container or speculative plugin discovery.
Existing MCP names/signatures remain the external compatibility contract.

Trade-off: full backend substitutability is incremental; the mature browser
adapter has a broader action surface than the currently typed core protocols.
New capability families should add focused protocols and contract tests rather
than growing a single universal driver interface.

## ADR 7 — Additive agent results and best-effort progress

Status: shipped in 0.8.0.

Keep existing tool names, arguments and text/image content; add a versioned
structured envelope with evidence supplied by the application layer. Do not
parse English messages to infer verification outcomes. A completed tool call
and a passing application check are separate concepts.

Emit fixed phase events rather than estimates or model reasoning. MCP progress
uses an ordinal without a total. Bound retained phases and delivery latency;
slow clients must not indefinitely stall testing. Hosts without visible progress
still receive the final phase summary.

Trade-off: text and structured data can duplicate information, and not all tools
have rich data payloads yet. Compatibility comes before claiming token savings.
Measure actual agent costs with paired trials before changing default verbosity.

## ADR 8 — Separate protocol controls from agent effectiveness

Status: shipped in 0.8.0.

Use exact externally adjudicated bug IDs, matched experimental controls, and
evidence-backed coverage. Reject incomplete pairs instead of silently dropping
failures. Keep unknown usage unknown and report duplicate findings separately.
Local deterministic smoke cases include buggy, fixed and clean variants and
do not call any model.

Trade-off: the scorer trusts supplied adjudication and cannot certify an external
experiment. Independent evidence review and sufficient repeated trials are
required before publishing claims about an agent's quality or efficiency.

## ADR 9 — Deterministic guidance without an embedded model

Status: accepted for 0.8.

Use bounded evidence-based next-check suggestions, post-action observations and
explicit unknown outcomes to support the host agent. Do not introduce a second
LLM planner, automatic writes, or guessed coverage. Keep conventional output as
the default; compact mode is an operator choice for hosts that consume structured
content. Model effectiveness must be measured separately from byte reduction.

Trade-off: hints are simple and can be inapplicable. The host remains responsible
for product semantics, user constraints and choosing the next action. A stalled
hint is a signal to reconsider, not an automatic stop or retry.

The optional `agent` profile narrows discovery to 19 common tools; `core` stays
the default (superseded by ADR 12). Atomic batch coverage avoids separate calls for goals sharing a
journey without inferring that those goals passed. Verification returns its page
observation, so a host can reuse evidence rather than observing reflexively.

## ADR 10 — Real-host collection with separately reviewed evidence

Status: accepted for 0.8.

Run an explicitly authorized, logged-in Codex CLI against an isolated MCP gateway.
Pin controls, limit attempts/calls/time/output, preserve failures and refuse API
fallback. Use independent tool/server traces for adjudication; reject missing
pairs or discarded claims. Baseline browser tools include post-action snapshots
so the comparison is not won by withholding basic browser ergonomics.

Trade-off: this measures one pinned host/model and synthetic task sample. The
small baseline adapter is not the official Playwright MCP. Host configuration
and fixture correctness require preflight checks; invalid preflights consume
budget and must remain visible in the evaluation report.

## ADR 11 — Bounded rechecks and explicit evidence provenance

Status: shipped in 0.10.0.

Use fresh-load symptom checks for observed persistence failures instead of
replaying writes. Recheck an existing finding by session ID at most twice,
preserving its earlier receipts and original replay trace. A confirmed finding
does not need another retry, and uncertainty must not be converted into a pass.

Capture fresh-load screenshots before restoring the caller's page. Keep stated
expected/actual outcomes and caller-claimed references separate from independent
verification. Close goal evidence ranges on focus changes; attach late findings
only through explicit goal associations, not semantic guesses. Final coverage
batches validate before teardown and do not infer completion from activity.

Trade-off: a reopened goal starts a new evidence range, and untagged deferred
findings cannot be reliably assigned to earlier journeys. The host must supply
the correct goal and requirements. A reproduced predicate still does not prove
a root cause or a semantic defect.

## ADR 12 — One complete default testing profile

Status: shipped in 0.11.0.

Context: the recommended 19-tool `agent` profile omitted `regression_check` and
`get_errors`, so following the recommendation broke the test, fix and re-check
loop, while the 30-tool `core` default carried long descriptions and two probes
that rarely belong in a first pass.

Decision: replace `agent` and `core` with a 28-tool `web` default that covers
exploration, actions, verification, findings, reports and regression rechecks
with concise descriptions. `advanced` adds network mocks, cookies and storage,
tabs, dialogs, waits, coordinate input, crawling and link/performance probes.
`screen` stays separate and `full` still exposes everything. The tool count
follows the workflow; capabilities move between profiles rather than being
deleted. Earlier names map to `web` so existing configurations keep starting.

Trade-off: earlier names guarantee that the server starts, not that the tool
set is unchanged. `agent` users see nine more tools and a larger discovery
payload (34,379 versus 24,386 bytes); `core` users lose `check_links` and
`check_performance` from the default (now in `advanced`) and receive a smaller
payload (versus 51,732 bytes on Python 3.11/3.12 or 50,564 on 3.13).
