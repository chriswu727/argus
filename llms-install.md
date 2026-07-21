# Install Argus Testing MCP

Argus is a local stdio MCP server for exploratory QA. Install the published
`argus-testing` package; do not clone the repository or start the LiteLLM-backed
`argus` CLI when the goal is MCP setup.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/) so the client can launch `uvx`

Install the Playwright Chromium browser once:

```bash
uvx --from playwright playwright install chromium
```

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

Run:

```bash
uvx --from argus-testing argus-mcp --version
uvx --from argus-testing argus-mcp --list-tools
```

The first command should report the current `argus-testing` version. The second
should report the default `core` profile with 29 tools, including
`start_session`, `observe`, `record_bug`, and `end_session`.

Then restart or refresh the MCP client and ask:

> Test my app at http://localhost:3000 — find real bugs.

## Optional profiles

- `--tool-profile core` is the 29-tool default for browser QA.
- `--tool-profile screen` exposes 14 native macOS testing tools and requires
  Screen Recording and Accessibility permissions.
- `--tool-profile full` exposes all 76 specialist browser and screen tools.

Do not add `--unsafe` unless arbitrary page-context JavaScript execution is
explicitly required and the target is trusted.
