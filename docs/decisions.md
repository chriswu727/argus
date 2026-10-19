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
