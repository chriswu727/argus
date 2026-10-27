# Argus — Testing for Coding Agents

Argus helps coding agents test the apps they build: exercise real user journeys,
find bugs, and verify them with evidence. This plugin connects Claude to the local
Argus MCP server. Its 28-tool web profile covers exploration, interaction,
verification, findings, reports, and regression checks.

This is a project-maintained plugin, not an Anthropic product. Providing this
package does not mean it has been accepted into Anthropic's directory.

## Requirements and setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and make
`uv` available to the application that starts Claude. The launcher selects Python
3.12 and runs the exact published `argus-testing==0.11.0` release from the bundled
`uv.lock`, which pins dependency versions, sources, and hashes. It refuses to run
if the project and lockfile disagree, and creates a local virtual environment
under the plugin directory.

Before starting Claude with the plugin for the first time, warm uv's cache and
install the matching Chromium browser. This works from any directory:

```bash
uvx --python 3.12 --from argus-testing==0.11.0 --with playwright==1.64.1 python -m playwright install chromium
```

The first plugin start otherwise downloads Python 3.12 and the locked
dependencies (a few hundred megabytes unpacked) before the MCP handshake, which
can outlast the client's startup wait on a slow connection. The warm-up pins
Argus and Playwright; uv resolves the other dependencies afresh rather than from
the plugin's `uv.lock`, so they usually, but not always, match it. A warm cache
therefore usually removes most of the first-start download. If the first connection still times
out, start Claude with a longer startup timeout in milliseconds, for example
`MCP_TIMEOUT=120000 claude`. Linux may also need Playwright's system
dependencies; see the [installation guide](https://github.com/chriswu727/argus#quick-start).
Browser installation is a separate, explicit setup step, not a hidden startup hook.

Install the plugin from this repository's marketplace (Claude Code 2.1.275 or
later):

```
/plugin install argus-testing --marketplace chriswu727/argus
```

On earlier versions, add the marketplace first:

```
/plugin marketplace add chriswu727/argus
/plugin install argus-testing@argus
```

To try a checkout for one session instead, run `claude --plugin-dir ./plugins/claude`.
Do not install a second Argus MCP connection alongside this plugin: use either
the plugin or a manual `claude mcp add` configuration.

Local MCP servers work in Claude Code and in Cowork sessions running on the
user's computer. They do not execute in claude.ai web chat. This package does not
provide a remote MCP endpoint, a custom slash-command skill, or lifecycle hooks.

## Test an application

Ask the agent to test an application you are authorized to exercise, for example:
"Test http://localhost:3000. Check saving, state after refresh, invalid input, and
cross-page consistency. Report reproducible issues with evidence."

Other example tasks, after starting your own application:

- "Test the form at http://localhost:3000 with blank and invalid input. Check
  that errors are useful and do not erase valid fields. Do not create real
  accounts or send messages."
- "Recheck the known findings for http://localhost:3000 using the existing
  local regression journal. Do not repeat write actions without approval.
  Distinguish still present, no longer reproduced, and inconclusive. An empty
  journal is not evidence that the app is correct."

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
