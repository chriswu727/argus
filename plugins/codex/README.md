# Argus — Testing for Coding Agents (Codex plugin)

Argus helps coding agents test the apps they build: exercise real user journeys,
find bugs, and verify them with evidence. This plugin connects Codex to the local
Argus MCP server with its 29-tool web profile, which covers exploration,
interaction, verification, findings, reports, regression checks, and Playwright
test export.

This is a project-maintained plugin, not an OpenAI product, and it is not listed
in OpenAI's plugin directory.

## Requirements and setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and make
`uv` available to Codex. The launcher selects Python 3.12 and runs the exact
published `argus-testing==0.12.0` release from the bundled `uv.lock`, which pins
dependency versions, sources, and hashes, in a virtual environment under the
installed plugin directory.

Before the first session, warm uv's cache and install the matching Chromium
browser. This works from any directory:

```bash
uvx --python 3.12 --from argus-testing==0.12.0 --with playwright==1.64.1 python -m playwright install chromium
```

The warm-up pins Argus and Playwright; uv resolves the other dependencies afresh
rather than from the plugin's `uv.lock`, so they usually, but not always, match
it. A warm cache therefore usually removes most of the first-start download.
Linux may also need Playwright's system dependencies; see the
[installation guide](https://github.com/chriswu727/argus#quick-start).

Add this repository's marketplace and install the plugin:

```bash
codex plugin marketplace add chriswu727/argus
codex plugin add argus-testing@argus
```

Plugins work in the Codex CLI and the ChatGPT desktop app; the Codex IDE extension
does not load plugins. Do not also configure Argus with `codex mcp add`: use either
the plugin or a manual MCP entry. `uvx --from argus-testing argus-mcp --doctor`
checks the browser and report directory if the first session fails.

## Test an application

The bundled `argus-test` skill guides a session: it agrees the scope and asks
before anything with real-world effects, checks the environment, tests, rechecks
symptoms on fresh loads, and reports findings grouped as verified, observed, not
reproduced, or inconclusive. Ask Codex to test an application you are authorized
to exercise, for example:
"Test http://localhost:3000. Check saving, state after refresh, invalid input, and
cross-page consistency. Report reproducible issues with evidence."

The server supplies its testing protocol through `start_session`. The agent must
respect the user's scope and obtain authorization before consequential actions.
A completed tool call is not proof that an application passed. Independent
verification, agent observations, non-reproduction, and inconclusive checks remain
distinct. The plugin does not enable native macOS screen control, arbitrary page
JavaScript, or the optional monitoring port.

## Privacy Policy

The [Argus privacy policy](https://github.com/chriswu727/argus/blob/main/PRIVACY.md)
covers data collection, storage, sharing, and retention. Argus has no operated cloud
service or telemetry collector and does not call a model itself. Codex and its model
provider receive the tool inputs and evidence included in a conversation, subject to
their own policies. Reports, screenshots, journals, and state capsules stay on the
user's computer until removed and may contain private application data.

Licensed under [MIT](https://github.com/chriswu727/argus/blob/main/LICENSE).
