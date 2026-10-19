# Privacy

Argus is a local-first testing tool. The project does not operate an Argus
cloud service, user account system, analytics endpoint, or telemetry collector.

## Data Argus handles

During a test, Argus may process page text, accessibility data, URLs, browser
console and network events, testing goals and constraints, verification targets,
cookies or storage when the corresponding tools are called, screenshots,
downloaded-file metadata, and native macOS accessibility information in screen
mode. Coverage reports omit typed and pasted input values from action references,
but goals, constraints, verification targets, finding titles, and screenshots are
evidence and may contain application data; do not put secrets in those fields.

Reports, screenshots, reproduction receipts, state capsules, and regression
journals are written locally under `./argus-reports` by default. Set
`ARGUS_OUTPUT_DIR` to choose another location. `argus-cleanup` can plan and
remove complete old report runs while leaving journals and state capsules
untouched; it is a dry run unless `--apply` is passed.

Captured request headers, response headers, request bodies, and inspectable API
response bodies are redacted for common credential-bearing keys before they are
retained. Response bodies are bounded, and binary or oversized bodies are not
loaded for inspection. These safeguards reduce common exposure paths but cannot
recognize every application-specific secret format.

JSON bodies are parsed and recursively redacted; multipart bodies are omitted
from inspection rather than retaining uploaded fields or file bytes. Known
credential-bearing URL parameters and headers are redacted in captured requests.
All built-in report formats use a common redacted copy of the report data,
including visited URLs and nested reproduction evidence. This does not modify
the live session or the journal's executable verification conditions. Journals
remain private local artifacts and may retain sensitive verification URLs.
Screenshots and unlabeled application-specific secrets still require manual review.

Cookie and storage inspection hides values unless the operator enables
`ARGUS_ALLOW_SENSITIVE_READS=1` before startup. This is separate from capsule
capture/restore, which intentionally preserves raw browser state. Local file
inputs require approved directories in `ARGUS_FILE_ROOTS`; see [Security](SECURITY.md).

## Where data can go

- The MCP host and its configured model provider receive the Argus tool inputs
  and outputs that the host includes in a conversation. Their privacy terms
  apply independently of Argus.
- Browser tests connect to the URLs the user or agent chooses to test, along
  with resources those pages load.
- CLI mode uses LiteLLM and sends planner context to the model provider selected
  by the user. MCP mode does not require an Argus API key or an Argus-operated
  model service.
- Installing or upgrading through PyPI, `pip`, or `uvx` contacts those package
  distribution services under their own policies.

Argus does not intentionally transmit test artifacts to the project maintainer.
Review reports and screenshots before sharing them because they can contain
sensitive application data.

## Replay inputs and state capsules

Typed, pasted and selected values are stored in a per-session in-memory vault.
Action traces retain opaque references; exports omit those references and mark
input-dependent steps `requires_input`. Closing, replacing, cancelling or timing
out a session clears its vault. This is reference removal, not a guarantee of
cryptographic erasure from Python memory, swap or process dumps. The host already
knows the inputs it supplied. Page content, human-authored evidence, prompts,
console messages and screenshots may independently reveal the same information.

State capsules intentionally contain raw cookies and browser storage. They use
atomic writes, schema validation, owner-only files on POSIX, and a defensive
`.gitignore`. They are not encrypted. They persist until explicitly deleted by
the user; ordinary report cleanup does not remove them. Do not share them.

## Optional local monitor and diagnostic logs

`--monitor-port` enables a read-only HTTP dashboard on `127.0.0.1` only. Status,
SSE events and metrics contain operation names, timestamps, state, error codes,
run IDs and counts, not tool arguments, page URLs, page text, input values or
images. The event ring is bounded and process-local, not durable audit storage.
Host/Origin validation reduces cross-site access; there is no authentication
against other local processes. Do not expose the port publicly.

Structured Argus diagnostics go to stderr, never the MCP stdout channel.
Operation diagnostics use allowlisted metadata rather than raw arguments or
exception tracebacks. Dependency diagnostics and explicit tool outputs have
their own formats. Review host logs before sharing them.
