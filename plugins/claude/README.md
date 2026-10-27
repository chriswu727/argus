# Argus — Testing for Coding Agents

Argus helps coding agents test the apps they build: exercise real user journeys,
find bugs, and verify them with evidence. This plugin connects Claude to the local
Argus MCP server. Its 28-tool web profile covers exploration, interaction,
verification, findings, reports, and regression checks.

This is a project-maintained plugin, not an Anthropic product. Providing this
package does not mean it has been accepted into Anthropic's directory.

## Requirements and setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and make
`uvx` available to the application that starts Claude. The launcher selects Python
3.12 and the exact published `argus-testing==0.11.0` release. uv may download that
interpreter and the Python dependencies when they are not already cached.

Before the first browser session, install the matching Chromium browser:

```bash
uvx --python 3.12 --from argus-testing==0.11.0 python -m playwright install chromium
```

Linux may also need Playwright's system dependencies; see the
[installation guide](https://github.com/chriswu727/argus#quick-start).
Browser installation is a separate, explicit setup step, not a hidden startup
hook. Do not install a second Argus MCP connection alongside this plugin: use
either the plugin or a manual `claude mcp add` configuration.

From a checkout, load the plugin for one Claude Code session:

```bash
claude --plugin-dir ./plugins/claude
```

Local MCP servers work in Claude Code and in Cowork sessions running on the
user's computer. They do not execute in claude.ai web chat. This package does not
provide a remote MCP endpoint, a custom slash-command skill, or lifecycle hooks.

## Test an application

Ask the agent to test an application you are authorized to exercise, for example:
"Test http://localhost:3000. Check saving, state after refresh, invalid input, and
cross-page consistency. Report reproducible issues with evidence."

The server supplies its testing protocol through `start_session`. It does not
start a test merely because the plugin is installed. The agent must respect the
user's scope and obtain authorization before consequential actions. A completed
tool call is not proof that an application passed. Independent verification,
agent observations, non-reproduction, and inconclusive checks remain distinct.

The plugin does not enable native macOS screen control, arbitrary page JavaScript,
or the optional monitoring port. Reports normally go to `./argus-reports` in the
server's working directory; `ARGUS_OUTPUT_DIR` can override that location.

## Privacy Policy

The [Argus privacy policy](https://github.com/chriswu727/argus/blob/main/PRIVACY.md)
covers data collection, storage, sharing, and retention. Argus has no operated
cloud service or telemetry collector and does not call a model itself. Claude and
its model provider receive the tool inputs and evidence included in a
conversation, subject to their own policies. Tests contact the selected websites
and the resources those sites load. Setup contacts uv's Python distribution
sources, PyPI, and Playwright's browser download services.

Reports, screenshots, journals, and state capsules remain on the user's computer
until removed. They may contain private application data. Capsules intentionally
retain raw browser credentials and are not encrypted; do not share them. Report
cleanup does not remove journals or capsules. Review artifacts before publishing.

## Documentation and support

- [Testing protocol and tool reference](https://github.com/chriswu727/argus#readme)
- [Security boundaries and private vulnerability reporting](https://github.com/chriswu727/argus/blob/main/SECURITY.md)
- [Support and bug reports](https://github.com/chriswu727/argus/issues)

Licensed under [MIT](https://github.com/chriswu727/argus/blob/main/LICENSE).
