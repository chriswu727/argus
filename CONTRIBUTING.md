# Contributing

Keep the primary README, code, documentation and release notes in English and
focused on product usage, implementation, verification and contribution guidance.
`README.zh-CN.md` is the dedicated translation exception; keep its version,
commands, capabilities and limitations synchronized with the English README.

Use Python 3.11 or newer. CI runs 3.11 and 3.12. Browser checks are local and do
not call a model provider:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip 'setuptools>=83.0.0'
pip install -e '.[dev]'
playwright install chromium
ruff check argus tests
ruff format --check argus tests
mypy
python -m pip_audit --vulnerability-service osv --progress-spinner off --skip-editable
ARGUS_REQUIRE_BROWSER=1 pytest -q
```

`mypy` covers the contracts, settings and runtime core listed in `pyproject.toml`;
the whole legacy codebase is not yet strictly typed. CI treats unavailable-browser
skips as failures. macOS AX permissions and real native-app behavior need a
separate macOS manual check; Linux CI does not claim to test that integration.

## Where changes belong

- Tool arguments, MCP metadata and profile selection: `entrypoints/mcp/`.
- A new QA operation: the appropriate `application/` service and `OPERATIONS`
  registry; add catalog metadata, both README versions and a protocol test.
- Pure rules and typed data: `domain/`; no browser, MCP or HTTP imports here.
- New verification: register an async strategy on `runtime.verifiers`. Return
  an explicit receipt with `attempted` and a three-state `reproduced` result.
  Automatic regression deliberately permits only read-only built-in checks.
- New report format: a pure renderer under `adapters/reporting/`, injected via
  `Reporter(exporters=...)` in `adapters/reporting/reporter.py`. HTML remains
  the primary report. `argus.models`, `argus.detector` and `argus.reporter` are
  compatibility re-exports only; import from `domain/` and `adapters/` instead.
- A different journal: implement `JournalPort` and wire it in `bootstrap.py`.
  The repository owns merge atomicity; callers should not read-then-write.
- Browser actions: add a public adapter method. Application services must not
  access `_page`, `_context`, `_locator` or other adapter private state.
- Monitor additions: `adapters/monitoring.py` and its packaged `assets/`.
  Never add raw inputs, arbitrary URLs, page contents or credentials to events.
- MCP result schemas: `entrypoints/mcp/results.py`; application evidence uses
  `ToolText` without importing MCP. Keep text/image compatibility and test real stdio.
- Progress: fixed phases in `application/progress.py`, emitted at actual work
  boundaries through the runtime. Never accept page-supplied progress messages.
- Coverage hints: `application/guidance.py`; use observed facts and bounded output,
  never model calls, automatic writes or inferred completion. Monitor only counts.
- Paired evaluation: `evaluation/`; keep model calls and external adjudication
  outside the product runtime. Preserve both arms, failures and unknown usage.
- Access rules: `access.py`; keep operator policy outside tool-controlled data.
  File tests must explicitly grant their temporary fixture directory.
- Redaction: `security.py` and the shared reporting privacy wrapper. Test every
  exporter with synthetic credentials and verify live objects remain unchanged.

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

Benchmarks and evaluations live in `argus_lab/`, which ships in the source
archive but not the installed wheel. For the scripted benchmark, start `test-site/app.py` and
`human-eye-fixture/app.py`, then run `python -m argus_lab.bench --target all`. This is
a capability ceiling, not a real-agent success rate. Model-provider benchmarks
consume subscription/API usage and must be explicitly authorized.

Run `python -m argus_lab.evaluation --smoke` for the isolated, no-model paired
protocol check. See [evaluation methodology](docs/evaluation.md) before reporting
agent effectiveness. Protocol details are in [agent protocol](docs/agent-protocol.md).

The opt-in `evaluation.codex_runner` consumes subscription usage and requires
explicit authorization. Never add it to CI. Unit and stdio gateway tests do not
invoke a model. Test every fixture's buggy/fixed ground truth through real browser
interactions before collecting model trials. Keep raw collections and credentials
outside the public repository; only reviewed synthetic aggregate evidence belongs here.

For release, synchronize `pyproject.toml` and both version fields in
`server.json`, update `CHANGELOG.md`, build with `python -m build`, check with
`twine check`, and smoke-test the wheel outside the source directory. JS and
dashboard assets are package resources and must be present in the wheel.
Source distributions use an explicit inclusion list. Run
`python scripts/check_distributions.py dist/*` after building; newly packaged
files must satisfy the public-content policy before publication.
Publish only after CI passes. The GitHub release workflow builds/tests again,
publishes to PyPI through Trusted Publishing, then publishes MCP Registry
metadata. Do not commit keys, capsules, generated reports or local test data.

Keep Glama's automatic release switch off: its build configuration can retain a
resolved commit even after the pin field is cleared. Before creating the GitHub
release, synchronize Glama's repository index and explicitly select the tested
main commit. Build without publishing first and verify the source SHA, installed
Argus version and MCP `serverInfo.version`. After the GitHub release and package
publication succeed, manually publish that verified Glama build under the same
version. A version label does not prove which source was built. Do not reuse an
occupied version label for a different build.

After publication, verify the PyPI wheel and source archive, official MCP
Registry package version, and directory release associations. Run
`python -m argus.install_check` against the published package outside the checkout.
MCPRush's default listing should expose the 28-tool web profile, not the
`evaluation` baseline tools; preserve manually declared schemas when refreshing
its source. Directory scans and version badges are not substitutes for these
installation and handshake checks.
