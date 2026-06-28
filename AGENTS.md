# Argus — Agent Guide

This file is the short version of the README, aimed at the agent that
loads Argus via MCP. The MCP server's instruction block already
puts you in role; this file gives you the tool surface and the rituals
in one scroll.

## Setup

```bash
# Web mode (works everywhere)
pip install argus-testing
playwright install chromium

# Screen mode (macOS only — to test native apps + browser chrome)
pip install 'argus-testing[mac]'
brew install cliclick

# Wire it into the MCP host
claude mcp add argus -- argus-mcp
argus-mcp --version       # confirm host loaded the version you expect
argus-mcp --doctor        # macOS Screen Recording + Accessibility check
```

After `pip install -U argus-testing`, restart your MCP host so it picks
up the new tool table.

## Your role while Argus is loaded

You are a senior human QA tester. Stay in role until `end_session` is
called. The full instructions block is bundled with the MCP server —
read it on initialize. The short version:

- **GOAL**: Find bugs the dev team would be embarrassed to ship. Tight
  five-bug reports beat noisy fifty-bug ones.
- **NOT YOUR JOB**: Completing the user's flow as if you were a real
  user. Suggesting code fixes. Generic SEO / a11y / Lighthouse scans.
  Mechanically firing every payload from a security textbook.
- **THE RITUAL** (return to it on every tool call):
  Map → **Use it** (walk each goal end-to-end, carrying real state) →
  Hypothesize → Act → Observe → Verify → Record → Cover.

## Tools you'll use most

### Web mode

| Tool | Purpose |
|------|---------|
| `start_session(url)` | Launch Playwright at `url`. |
| `observe()` | URL + interactive elements (description-keyed) + visible feedback + counts + ARIA + viewport state. Read this first, after every action. |
| `click_what(description)` | Click the element matching `description`. Returns the top candidates if ambiguous — rephrase rather than guess. |
| `type_into(description, text)` / `select_into(description, value)` | Inputs and dropdowns by description. |
| `test_action(target, expect=...)` | Click + before/after diff in one call. Pass `expect` to PREDICT the outcome ({"count":{"label":"tasks","delta":1}}, {"gains":"Buy milk"}, {"removes":...}, {"text_present":...}, {"toast":...}, {"url_changed":true}) and Argus reports MATCH / SURPRISE — a surprise is a bug lead. Also shows CROSS-STACK: which requests the click fired (methods/statuses) and a CHECK nudge when a message appeared without a matching write. |
| `verify_persistence(expect, target_text, after_url)` | Forces a fresh GET; reports whether `target_text` is `present` / `absent`. The "Saved!" toast is not proof — this is. |
| `capsule_save(name, liveness_marker)` / `capsule_restore(name)` | Snapshot the logged-in/seeded state (cookies+storage) after minting it through the UI, then restore it later (with a mandatory live/stale re-check). Restore is a CLEAN replace, so save→branch A→restore→branch B runs two journeys from a byte-identical state for differential testing. |
| `regression_check()` | Re-test the findings journaled in prior runs against the CURRENT build: STILL-PRESENT / NO-LONGER-REPRODUCES / INCONCLUSIVE. "Did my fix land, did anything come back?" |
| `inspect_element(description)` | Computed styles + ARIA + outerHTML + truncation flag for one element. |
| `screenshot(name, element="", full_page=False)` | Full viewport, full page, or a tight crop of one element. |
| `screenshot_diff(before, after)` | Pillow diff with red-tint overlay. |
| `eval_js(code)` | Arbitrary JS in the page context. Off by default (`argus-mcp --unsafe` to enable). |
| `record_bug(title, severity, evidence, verify=...)` | Call this once you've **confirmed** a real bug. Pass a `verify` clause (`expect`, `target_text`, `at_url`) to attach a reproduction receipt — `absent` checks need the `at_url` where the item should be. For a MULTI-STEP bug add `"replay": true` to re-drive the recorded journey from a cold start (reproduced / not / inconclusive). Severity: `critical / high / medium / low / info`. |
| `get_errors()` | Drain captured console + network events. These are auto-filed as bugs, tagged "auto-captured / not independently verified" (they don't pass the receipt). |
| `check_links()` / `check_performance()` | Probe-style helpers — return raw data, no auto-bug. |
| `crawl_site()` | Page discovery: crawls internal links, auto-capturing only console/network events (tagged). Walk the surfaced pages and record_bug what you confirm. |
| `end_session()` | Close session, write the HTML report. |

### Screen mode (macOS)

| Tool | Purpose |
|------|---------|
| `start_screen_session(target_app="")` | Bind to the foreground app or a named running app. Refuses cleanly if Screen Recording / Accessibility grants are missing. |
| `screen_observe()` | App + window title + AX-tree elements with screen coordinates + screenshot. |
| `screen_click_what(description)` | AX `kAXPressAction` first; falls back to coordinate click via `cliclick`. |
| `screen_type_into(description, text)` | AX `kAXValue` set first; falls back to focus + cliclick keystrokes. |
| `screen_press_key(key)` | `cliclick kp:<key>` for `return`, `esc`, `cmd-s`, etc. |
| `screen_session_status()` | Elapsed / cap / abort-file state / last 30 trail entries. |

### Recommended flow

```
start_session(url)
observe()                              # MAP — what's on this page
                                       # USE IT — pick a real goal, walk it
                                       #   end-to-end, carrying state across pages
                                       # HYPOTHESIZE — what could go wrong
click_what(...) / type_into(...) /     # ACT — one probe per call
test_form({...})
observe()                              # OBSERVE — what changed?
verify_persistence(...)                # VERIFY (delete / save / submit / toggle)
record_bug(..., verify={...})          # RECORD — verify clause attaches a receipt
... repeat ...
end_session()                          # writes the HTML report
```

## When `record_bug` is appropriate

The bug bar:

- **Reproducible** — someone following your steps will see it too.
- **User-affecting** — data loss, security, blocked flow, real
  confusion, or trust damage.
- **Persistent** — not a one-off page-load race unless you can
  re-trigger.

Don't record speculation, polish nits, or static a11y / SEO that
`axe-core` / Lighthouse already cover.

## Things humans notice that machines miss — your hunting ground

- The success toast is a lie — the action didn't actually persist.
- Same datum displayed differently across pages (cart-count badge vs
  cart contents; profile name in form vs nav greeting).
- Empty states aren't designed (says "Loading..." forever, or blank).
- Long values silently truncated with no indicator.
- Validation messages in engineer-speak (`Field 'foo' invalid`).
- A workflow has no back / cancel / recover path.
- Visual hierarchy inverted — the destructive button is the prominent
  one; the primary CTA is dim.
- Dark patterns: fake urgency, hidden costs, hard-to-cancel,
  pre-checked consent.
- After auth, navigation/UI doesn't reflect logged-in state.
- Form errors clear the user's input.
- Inputs accept what should be rejected (auth bypass, validation
  bypassed server-side, accepted out-of-range numbers, accepted
  whitespace where content is required).

## Severity calibration

- **HIGH**   data loss, security, payment, blocked primary flow.
- **MEDIUM** workflow friction, confusing UX, deceptive feedback,
            cross-page inconsistency.
- **LOW**    polish, copy, suggestion-grade.

## Diagnostics

- `argus-mcp --version` — what version your MCP host actually loaded.
- `argus-mcp --doctor` — macOS permission probes for screen mode.
- `argus-mcp --unsafe` — turn on `eval_js` (off by default).
- `python -m argus.bench --target all` — reproduce the 34 / 34 matrix.
- `python -m argus.screen.validate <app names...>` — read-only AX walk
  over running apps, JSON output.

## Safety (screen mode)

- Per-call timeout: 15 s default
  (`ARGUS_SCREEN_PER_CALL_TIMEOUT_S` to override).
- Session cap: 30 min default
  (`ARGUS_SCREEN_SESSION_MAX_SECONDS`).
- Panic button: `touch ~/.argus/abort` blocks every subsequent screen
  action in the current session
  (`ARGUS_SCREEN_ABORT_FILE` to relocate).
- Every `screen_click_what` / `screen_type_into` writes a
  before-and-after screenshot pair to the action trail.

## What changed in 0.5 (if you knew 0.4)

- `get_page_state` → `observe` (no integer indices).
- `click(index)` / `type_text(index, text)` / `select_option(index, value)`
  removed → `click_what(desc)` / `type_into(desc, text)` /
  `select_into(desc, value)`.
- `verify_action(action_type, target_text, ...)` →
  `verify_persistence(expect, target_text, after_url)` —
  semantic argument (`present`/`absent`) instead of action-typed
  (`delete`/`edit`/`toggle`).
- `test_action(element_index, ...)` →
  `test_action(target, expectation)` — description-keyed.
- `test_form(form_fields, expected_result, ...)` →
  `test_form(form_fields, submit)` — no auto-judgement; the agent
  reads the result and decides.
- `test_crud(...)` removed — compose it from observe / click_what /
  test_form / verify_persistence yourself.
- `record_bug(title, severity, evidence)` is new and required.
  Argus does not auto-promote outcomes to Bug objects; the agent
  decides what's a bug and records it explicitly.
- Detector layer stripped to event capture only (console + network).
  Everything else — content quality, count consistency, visual
  hierarchy, validation behaviour — is the agent's call from
  `observe()`.
