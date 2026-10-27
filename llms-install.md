# Install Argus Testing MCP

Argus is a local stdio MCP server for exploratory QA. Install the published
`argus-testing` package; do not clone the repository when the goal is MCP setup.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) so the client can launch `uvx`

Install the Playwright Chromium browser once:

```bash
uvx --from playwright playwright install chromium
```

After an upgrade, a missing browser executable can mean the installed Chromium
does not match Argus's resolved Playwright version. Install the matching browser
with `uvx --from argus-testing python -m playwright install chromium`, then restart
the MCP host.

## MCP configuration

Use this server entry in any client that accepts the standard `mcpServers`
shape:

```json
{
  "mcpServers": {
    "argus": {
      "command": "uvx",
      "args": ["--from", "argus-testing", "argus-mcp"]
    }
  }
}
```

Equivalent client commands:

```bash
claude mcp add argus -- uvx --from argus-testing argus-mcp
codex mcp add argus -- uvx --from argus-testing argus-mcp
```

For Cursor, use the standard JSON above or the one-click button in the project
README.

## Verify the installation

The current release is 0.10.0. For reproducible setup, replace `argus-testing`
with `argus-testing==0.10.0` in the launch arguments. Restart the MCP host after
upgrading; a running server retains its loaded version and tool schema.

Run:

```bash
uvx --from argus-testing argus-mcp --version
uvx --from argus-testing argus-mcp --list-tools
```

The first command should report the current `argus-testing` version. The second
should report the default `core` profile with 30 tools, including
`start_session`, `observe`, `coverage_update`, `record_bug`, and `end_session`.

Then restart or refresh the MCP client and ask:

> Test my app at http://localhost:3000 — find real bugs.

## Optional profiles

- `--tool-profile agent` exposes 19 common browser-testing tools with concise
  descriptions. Pair with `--response-mode compact` for structured-result-aware hosts.
- `--tool-profile core` is the 30-tool default for browser QA.
- `--tool-profile screen` exposes 14 native macOS testing tools and requires
  Screen Recording and Accessibility permissions.
- `--tool-profile full` exposes all 77 specialist browser and screen tools.

Do not add `--unsafe` unless arbitrary page-context JavaScript execution is
explicitly required and the target is trusted.

For a host that consumes MCP `structuredContent`, add `--response-mode compact`
to avoid duplicated prose for migrated testing tools. Keep the default
`compatible` mode for older hosts. Add `--monitor-port 8765` for a read-only
loopback dashboard; MCP phase notifications require host support.

Version 0.9 also offers `--response-mode delta` for hosts that retain observation
snapshots and validate their base IDs. Use `observe` to resynchronize after a
missing base. Do not enable it for a host that ignores structured results or
cannot retain state. It preserves verification outcomes and requested images;
encoding savings do not establish model token savings.

Run `uvx --from argus-testing python -m argus.install_check` for a no-model,
no-browser check of installed metadata, product-version handshake, tool discovery
and structured errors.

Argus is useful when an agent needs evidence-linked journey coverage,
persistence checks, independent reproduction receipts, or machine-readable
regression results. It is not a guarantee of comprehensive testing or zero false
positives. See the repository's evaluation methodology before quoting performance.
