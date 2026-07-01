# Argus

> An opinionated MCP server that turns your loaded LLM into a senior
> human QA tester — for web apps and any macOS app on your screen.

```
$ python -m argus.bench --target all

  buggytasks    22 / 22  = 100 %  in  20.5s
  darkshop      12 / 12  = 100 %  in   8.4s
  ──────────────────────────────────────────
  total         34 / 34  = 100 %  in  28.9s
```

The `34 / 34` is reproducible from `git clone` in two commands. The
**point** of Argus is the prompt + the tool surface: when an Opus-class
agent (Claude Code, Cursor, etc.) loads this MCP, it stops being "an
assistant with browser tools" and starts behaving like a QA tester —
hypothesising, observing, verifying persistence, recording reproducible
bugs, and refusing to wander off into "let me just complete your flow
for you".

The other thing that makes Argus different from the existing browser-MCP
crowd: **screen mode**. Same description-keyed tools, but the target is
whatever app is foreground on macOS — Notes, Cursor, Safari, your
in-progress feature. No headless Chrome, no scripted Playwright. Argus
sees what you see.

[Skip to Quick start](#quick-start) · [Bench](#bench-method) · [Tool
surface](#tool-surface) · [Philosophy](#philosophy)

---

## What it is, in one paragraph

Argus is an [MCP server](https://modelcontextprotocol.io/) that exposes
two things:

1. A **role-binding instructions block** that tells whichever agent
   loaded it: "while I'm here, you are a senior QA tester. Stay in role
   until end_session."
2. A **mode-agnostic tool surface** — `observe`, `click_what`,
   `type_into`, `verify_persistence`, `record_bug`, plus the same again
   for screen-mode (`screen_observe`, `screen_click_what`, …) — that
   the agent uses to drive whatever you point it at.

That's the whole product. There's no detector library, no AI brain
wrapped around static rules, no scoring. The agent is the smart layer.
Argus is an opinionated, well-instrumented seat to put that agent in.

## What it isn't

- **Not** an assertion library. There's no `expect(x).toBe(y)`. The
  agent reads page state and decides what's a bug.
- **Not** an axe / Lighthouse replacement. We deliberately don't run
  static a11y / SEO / performance scans — those tools already exist and
  are excellent. Argus only flags what *requires* human judgement to
  see.
- **Not** a task-completion agent. If you want it to actually buy the
  thing or send the email, use Browser-Use or Stagehand. Argus's
  instructions block specifically refuses task completion in favour of
  testing the flow.

## Quick start

```bash
# Web mode (works everywhere)
pip install argus-testing
playwright install chromium

# Screen mode (macOS only, optional)
pip install 'argus-testing[mac]'
brew install cliclick    # for keystroke / coordinate fallback

# Wire it into Claude Code
claude mcp add argus -- argus-mcp

# Confirm the version your MCP host will load
argus-mcp --version
```

> **After upgrading Argus**, restart your MCP host (Claude Code,
> Cursor, etc.). MCP hosts cache the tool table at startup, so a fresh
> `pip install -U argus-testing` won't expose new tools until the host
> reconnects to the server. `argus-mcp --version` is the easy way to
> verify which version your host is actually running.

Then, in your Claude Code / Cursor / any-MCP session:

```
"Test my app at http://localhost:3000 — find five real bugs."
```

For screen mode, say "test whatever is on my screen" or specify the
app:

```
"Test the Notes app in screen mode."
```

### Permission check (screen mode)

Screen mode needs Screen Recording + Accessibility grants. Run:

```
argus-mcp --doctor
```

It probes both, reports status, and gives you the
`x-apple.systempreferences:` deep-link for any missing grant.

### Regression in CI (zero-LLM)

Findings recorded with a verify clause are journaled per origin when a session
ends. Re-test them against a fresh build without an LLM — exits non-zero if any
previously-confirmed bug is still present, so CI can gate on it:

```bash
argus-regression http://localhost:3000
# STILL-PRESENT / FIXED / INCONCLUSIVE per finding; non-zero exit on STILL-PRESENT
```

### Reproduce the bench

```bash
# Start the seeded fixtures
python test-site/app.py             # BuggyTasks   :5555
python human-eye-fixture/app.py     # DarkShop     :5556

# Run all scenarios
python -m argus.bench --target all \
    --json bench-results/matrix.json \
    --md   bench-results/matrix.md
```

See [`bench-results/matrix.md`](bench-results/matrix.md) for the
checked-in artifact.

---

## Bench method

Argus's headline number — `34 / 34` — measures Argus's **capability
ceiling**. Each scenario is a deterministic Python sequence that
exercises the same MCP tools an LLM agent would call. We're answering
*"what's findable through this surface?"* — separate from
*"how often does any specific LLM remember to call the right tool?"*

### Real-LLM bench (honest recall)

`python -m argus.bench.agent_runner` (set a provider key, e.g.
`DEEPSEEK_API_KEY`, and `BENCH_MODEL`) has an **actual model** drive the tools
and scores recall/moat-engagement/cost across N trials — the true agent number,
not the ceiling. Four honest findings from running it on BuggyTasks (all with a
hard cost cap, ALL trials reported; total spend across every run below was under
¥2):

1. **Real recall is far below the `34/34` ceiling** — a real driver finds a
   handful of the 22 per pass, not all of them. The ceiling is what's
   *findable*; this is what a model *finds*.
2. **Variance is huge — don't rank models on a few runs.** An early 3-trial
   pass looked like a tidy "stronger model finds ~2× more" curve; 5 trials
   erased it (`deepseek-v4-pro` even came out *lowest* one run, two passes
   finding 0). Per-trial recall swings from 0 to 9. Report the spread, not a
   single number.
3. **Dogfooding the bench found bugs in Argus itself.** The transcripts
   revealed `record_bug` *crashing* on a string `evidence` arg (weaker models
   pass one) — silently dropping confirmed findings — plus resolver misses on
   `link "Tasks"` / `checkbox next to X`. Fixing those roughly **doubled**
   `deepseek-chat`'s mean recall (~2.5 → ~5/22 across two 6-trial runs, still
   high variance) and eliminated the crashes. The tool testing tool got tested.
4. **The precision moat is opt-in, and adoption scales with the driver.** A
   weak model rarely attaches a `verify` clause on its own. Safe nudges (an
   imperative RECORD instruction, accepting the target via `evidence`, a
   no-receipt reminder) took `deepseek-chat` from **0** verified findings to
   engaging in most trials — without ever guessing the symptom (which would
   risk a false VERIFIED). Reliable engagement still favors a capable driver.

### BuggyTasks (mechanical bugs)

22 seeded bugs in a small task-management app: console errors, dead
links, fake delete (UI says "deleted!" but data persists on refresh),
auth bypass, NaN dates, count-off-by-one, race conditions, etc. These
are the "scripted E2E could find them" bugs.

### DarkShop (human-eye bugs)

12 seeded bugs in a polished-looking e-commerce fixture: hardcoded
"Only 3 left!" scarcity, fake `-50%` sale badges where original price
equals sale price, "free shipping over $50" banner contradicted by a
flat `$5` in checkout, visual hierarchy inverted ("Add to Cart" demoted
while "Subscribe to Newsletter" gets the prominent green button), cross
-page state drift (rename succeeds on /account, navbar greeting still
shows the old name), and so on. **Static analysis catches roughly none
of these.** They require an agent that observes the page and *reasons*
about what's wrong.

### What an agent has to do per scenario

Take BUG #10 in DarkShop: the navbar greeting goes stale after an
account rename. The scenario does:

```
reset(mode="renamed")              # fixture pre-stages a renamed account
observe()                          # read the rendered /account page
                                   # — page shows "Alex-Renamed" in the form
                                   # — navbar still says "Hi, Alex"
record_bug(
    title="Account name change does not update nav greeting",
    severity="medium",
    evidence={"bug_type": "ux_issue", ...},
)
```

The judgement (*"the navbar saying Alex while the form says
Alex-Renamed is wrong"*) lives in the agent. The bench measures whether
Argus's surface gives the agent enough information to make that call.

### Screen mode

Screen mode is **not in the recall matrix** — that needs a seeded
macOS app with intentional bugs, which is out of scope for v1. Screen
mode is validated separately via `python -m argus.screen.validate`,
which walks the AX tree of any running app and reports the elements
+ round-trip identity probes. The checked-in artifact at
`bench-results/screen_validation.json` walks Notes (8 menu-bar
items, all localised OS strings — 5 / 5 unique probes).

To exercise screen mode against your own apps:

```bash
python -m argus.screen.validate Finder Notes "Google Chrome" \
    --json /tmp/screen.json
```

The script is read-only — it does not click, type, or move the mouse.
Output element counts vary by app: simple system apps expose a few
items at the menu-bar level; richer apps (browsers, IDEs) typically
expose tens to hundreds.

---

## Tool surface

### Web mode

| Tool | Purpose |
|------|---------|
| `start_session(url)` | Launch a Playwright session at `url`. |
| `observe()` | URL + title + interactive elements (description-keyed, no integer indices) + counts + visible feedback + ARIA tree + viewport state. |
| `click_what(description)` | Click the element best matching `description`. Returns the top candidates if ambiguous, rather than guessing. |
| `type_into(description, text)` | Resolve a text input by description, then type. |
| `select_into(description, value)` | Resolve a `<select>` by description, then choose. |
| `verify_persistence(expect, target_text, after_url)` | Force a fresh GET on `after_url` and report whether `target_text` is *present* or *absent*. The "Saved!" toast is not proof of persistence; this is. |
| `inspect_element(description)` | Computed styles + ARIA + outerHTML + truncation detection for one element. |
| `screenshot(name, element?)` | Full viewport, full page, or a tight crop of one element. |
| `screenshot_diff(before, after)` | Pillow-based pixel diff with red-tint overlay. |
| `eval_js(code)` | Arbitrary JS in the page context. Off by default; enable with `--unsafe` or `ARGUS_UNSAFE_EVAL=1`. |
| `record_bug(title, severity, evidence)` | The agent calls this after it confirms a real bug. Required: severity in `{critical, high, medium, low, info}`. |
| `get_errors()` | Drain captured console + network events (the only channels not visible in `observe`). |
| `check_links()` / `check_performance()` / `crawl_site()` | Probe-style helpers — return raw data, no auto-bug. |
| `end_session()` | Close session, write the HTML report. |

### Screen mode (macOS)

| Tool | Purpose |
|------|---------|
| `start_screen_session(target_app="")` | Bind to the foreground app or to a named running app. Refuses cleanly with deep-link permission instructions if grants are missing. |
| `screen_observe()` | Foreground app + window title + AX tree (capped at 200 elements / 6-deep) + screen-coords for every element + screenshot. |
| `screen_click_what(description)` | Resolve via AX tree; click via `kAXPressAction` first, fall back to `cliclick` coordinate click at the element centre. |
| `screen_type_into(description, text)` | Resolve via AX tree; set `kAXValue` first, fall back to focus + `cliclick` keystrokes. |
| `screen_press_key(key)` | `cliclick kp:<key>` for `return`, `esc`, `space`, `cmd-s`, etc. |
| `screen_session_status()` | Elapsed time vs cap, action count, abort-file state, last 30 trail entries. |

### Safety

Screen mode runs against the user's actual machine, so:

- **Per-call timeout** — every action wraps in a 15-second budget
  (`ARGUS_SCREEN_PER_CALL_TIMEOUT_S` to override). A hung AX query
  doesn't lock up the agent.
- **Session cap** — 30-minute default
  (`ARGUS_SCREEN_SESSION_MAX_SECONDS`). After expiry, all screen
  tools refuse with a clear "start a new session" message.
- **Abort file** — `touch ~/.argus/abort` blocks every subsequent
  screen action in the current session. Robust panic button that
  works from any second terminal.
- **Action trail** — every screen action records a paired
  before/after screenshot, automatically.

---

## Philosophy

This section exists because the design choices are opinionated.

### Trust the agent, don't simulate intelligence

The agent loaded into Argus is assumed to be Opus-class or stronger.
Static rules that pretend to *be* the smart layer are subtractive: they
add maintenance, produce false positives, and pull attention away from
what the agent actually saw. So Argus's `detector.py` is 130 lines —
it only captures the two channels the agent literally cannot see (the
console event stream and the HTTP layer).

Everything else — "is the page text broken", "is there a count
mismatch", "is this a misleading success toast", "is the visual
hierarchy wrong" — the agent reads from `observe()` and decides.

### Lock the role; don't bake a checklist

The MCP's instructions block does *not* tell the agent to fire every
XSS payload from a textbook. Smart agents don't need that and benefit
from being kept in role rather than handed instructions. The block
defines a senior-tester worldview (Map → Hypothesize → Act → Observe
→ Verify → Record → Cover), bug bar (reproducible, user-affecting,
persistent), severity calibration, and a hunting list of "things humans
notice that machines miss" — and gets out of the way.

### Description-keyed tools

`click_what("Login button")`, not `click(7)`. Element indices are how
dumb LLMs were prompted in 2023; they're a leaky abstraction even
within a single `observe`. A smart agent describes what it wants to
interact with by what it *is*, and Argus's resolver maps that to the
right element — refusing to misclick on ambiguity rather than guessing.

### Test anything on screen

The web is one target. Real software is hundreds of native macOS apps,
Electron things, IDEs, design tools, mobile simulators. Argus's screen
backend uses the macOS Accessibility tree as its structured surface and
`screencapture` for pixels — same description-keyed tools, no
framework lock-in. v1 is macOS-only; Win/Linux is v2.

---

## Project layout

```
argus/
├── argus/
│   ├── mcp_server.py          # tool surface + role instructions
│   ├── browser.py             # Playwright web backend
│   ├── detector.py            # console + network event capture (only)
│   ├── differ.py              # state diff for compute_changes
│   ├── resolver.py            # description → element resolver (web + screen)
│   ├── reporter.py            # HTML session report
│   ├── models.py              # Bug / PageState / etc.
│   ├── bench/
│   │   ├── runner.py          # fixture-agnostic harness
│   │   ├── scenarios_buggytasks.py
│   │   └── scenarios_darkshop.py
│   └── screen/
│       ├── permissions.py     # Screen Recording / Accessibility probes
│       ├── backend.py         # AX tree + cliclick + screencapture
│       ├── safety.py          # timeouts, abort file, action trail
│       └── validate.py        # read-only walker for real apps
│
├── test-site/                 # BuggyTasks fixture (22 mechanical bugs)
├── human-eye-fixture/         # DarkShop fixture (12 human-eye bugs)
├── tests/                     # unit + live tests (resolver, receipt, bench, browser, lifecycle, …)
└── bench-results/             # checked-in artifacts (json + md)
```

## Fixture convention

Argus benchmarks against fixtures that expose two HTTP endpoints:

```
GET  /api/test/state           # full in-memory state JSON
POST /api/test/reset?mode=...  # restore to a known starting state
```

`mode` is fixture-defined. BuggyTasks supports
`seeded` / `empty` / `all_done` / `one_pending`. DarkShop supports
`seeded` / `with_items` / `renamed`. See
[`docs/FIXTURE_CONVENTION.md`](docs/FIXTURE_CONVENTION.md) for the
full spec.

## Roadmap

Concrete next-up:

- **Real-world OSS PR** — file a real bug report on a real OSS web
  app, with Argus's run as the evidence trail.
- **Live LLM bench mode** — `python -m argus.bench --agent <model>`
  swaps the scripted driver for a real LLM, so we measure variance on
  top of capability ceiling.
- **Screen-mode seeded fixture** — a deterministic macOS app with
  intentional bugs, so the matrix becomes 2 × 2 and screen-mode recall
  is measurable.
- **VLM resolver fallback** — for apps with empty AX trees (some
  Electron things), use vision to resolve descriptions to coordinates.

## License

MIT. See [LICENSE](LICENSE).

## Author

Built by [Yichen Wu](https://github.com/chriswu727). Issues and PRs
welcome.
