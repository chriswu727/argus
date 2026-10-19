# Contributing

Keep public repository content in English and focused on product usage,
implementation, verification and contribution guidance.

Use Python 3.10 or newer. CI runs 3.10 and 3.12. Browser checks are local and do
not call a model provider:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
ruff check argus tests
ruff format --check argus tests
mypy
ARGUS_REQUIRE_BROWSER=1 pytest -q
```

`mypy` covers the contracts, settings and runtime core listed in `pyproject.toml`;
the whole legacy codebase is not yet strictly typed. CI treats unavailable-browser
skips as failures. macOS AX permissions and real native-app behavior need a
separate macOS manual check; Linux CI does not claim to test that integration.

## Where changes belong

- Tool arguments, MCP metadata and profile selection: `entrypoints/mcp/`.
- A new QA operation: the appropriate `application/` service and `OPERATIONS`
  registry; add catalog metadata, README documentation and a protocol test.
- Pure rules and typed data: `domain/`; no browser, MCP or HTTP imports here.
- New verification: register an async strategy on `runtime.verifiers`. Return
  an explicit receipt with `attempted` and a three-state `reproduced` result.
  Automatic regression deliberately permits only read-only built-in checks.
- New report format: a pure renderer under `adapters/reporting/`, injected via
  `Reporter(exporters=...)`. HTML remains the primary report.
- A different journal: implement `JournalPort` and wire it in `bootstrap.py`.
  The repository owns merge atomicity; callers should not read-then-write.
- Browser actions: add a public adapter method. Application services must not
  access `_page`, `_context`, `_locator` or other adapter private state.
- Monitor additions: `adapters/monitoring.py` and its packaged `assets/`.
  Never add raw inputs, arbitrary URLs, page contents or credentials to events.

Each runtime is explicit. Use `create_runtime(Settings(...))` and
`runtime.execute(name, operation, ...)`; do not set global environment variables
to switch sessions. The context variable is only the invocation-local bridge
for existing service signatures. `mcp_server` compatibility imports are not the
application API for new code.

## Verification and releases

Lifecycle tests use fake backends to deterministically force failures and
cancellation. Browser tests use temporary local fixtures. `test_stdio_protocol.py`
launches the real entry point and performs initialize/list/call/image/end over
MCP stdio. Monitoring tests use a real loopback socket and SSE disconnects.

For the scripted benchmark, start `test-site/app.py` and
`human-eye-fixture/app.py`, then run `python -m argus.bench --target all`. This is
a capability ceiling, not a real-agent success rate. Model-provider benchmarks
consume subscription/API usage and must be explicitly authorized.

For release, synchronize `pyproject.toml` and both version fields in
`server.json`, update `CHANGELOG.md`, build with `python -m build`, check with
`twine check`, and smoke-test the wheel outside the source directory. JS and
dashboard assets are package resources and must be present in the wheel.
Publish only after CI passes. The GitHub release workflow builds/tests again,
publishes to PyPI through Trusted Publishing, then publishes MCP Registry
metadata. Do not commit keys, capsules, generated reports or local test data.
