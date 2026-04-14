# Argus

**AI-powered exploratory QA agent.** Give it a URL, it explores your app like a real user — clicking buttons, filling forms, trying edge cases — and finds bugs that scripted tests miss.

Unlike Playwright or Cypress, you don't write test scripts. Argus **discovers bugs you didn't think to test for.**

## Core Feature: Auto-Verification

Argus's killer feature: **every action is automatically verified.** When you delete an item and see "Deleted!", Argus refreshes the page to check if it's actually gone. When you edit and save, Argus verifies the new value persisted. No other testing tool does this automatically.

```
test_form({"email": "test@test.com", "password": "wrong"}, expected_result="validation_error")

→ UNEXPECTED — form accepted input that should have been rejected
  (Auth bypass: any credentials accepted)
```

```
test_crud(create_url="/tasks/new", list_url="/tasks", item_data={"title": "Buy milk"})

→ CREATE: [OK] item created and found on list
  EDIT:   [BUG] "Buy milk (edited)" not found — edit did not persist!
  DELETE: [BUG] item still present after refresh — delete is fake!
```

## Quick Start (MCP Server for Claude Code)

Claude Code becomes the AI brain — no API key needed.

```bash
pip install argus-testing
playwright install chromium
claude mcp add argus -- argus-mcp
```

Then in Claude Code:

> "Test my app at http://localhost:3000, focus on the checkout flow"

### MCP Tools (18)

**Compound tools (core — do more per call):**

| Tool | What it does |
|------|-------------|
| `test_action(index, desc)` | Click + auto-capture before/after state + diff + detect bugs |
| `test_form(fields, submit)` | Fill form + submit + verify success/error + detect bugs |
| `test_crud(create, list, data)` | Full create/edit/delete cycle with auto-verification per step |

**Scanning tools:**

| Tool | What it does |
|------|-------------|
| `crawl_site(max_pages)` | Auto-crawl entire site, run all detectors on every page |
| `check_links()` | Crawl internal links, find 404s/5xx |
| `check_performance()` | Measure load time, find large resources |

**Low-level tools (for edge cases):**

| Tool | What it does |
|------|-------------|
| `start_session(url)` / `end_session()` | Launch/close browser, generate report |
| `get_page_state()` | See elements + page text + counts + toasts + meta + a11y |
| `click` / `type_text` / `select_option` / `navigate` / `go_back` / `scroll_down` | Direct interaction |
| `screenshot(name)` | Capture the current page |
| `get_errors()` | Run all 12 passive detectors |
| `verify_action(type, text, url)` | Manual verification of delete/edit persistence |

## What It Detects (16 types)

| Category | What it finds |
|----------|--------------|
| **Logic Bugs** | Fake delete/edit (says success but data didn't persist), misleading toasts |
| **Runtime Errors** | Console exceptions, HTTP 4xx/5xx, crashes |
| **Data Issues** | Count mismatches, broken dates, NaN, eternal "Loading..." |
| **Dead Links** | Crawls all internal links, finds 404s and 5xx |
| **Broken Images** | Images that failed to load |
| **SEO** | Missing meta description, OG tags, heading hierarchy |
| **Accessibility** | Missing alt text, unlabeled inputs, no lang attribute |
| **Performance** | Slow loads (>3s), large resources (>500KB), excessive requests |
| **Security** | Mixed content (HTTP on HTTPS), XSS reflection |

## Tested On

| Site | Type | Result |
|------|------|--------|
| React.dev | Next.js SPA | 2 bugs (a11y) |
| Angular.dev | Angular SPA | 1 bug (a11y) |
| Vue.js | Vitepress SPA | 1 bug (a11y) |
| TodoMVC Svelte | Svelte SPA | 2 bugs (SEO, a11y) |
| Tailwind CSS | Next.js | 9 bugs (a11y, perf, large resources) |
| Hacker News | Static | 5 bugs (SEO, a11y) |
| citymedicalaesthetics.com | Static | 8 bugs (dead links, 404 images, SEO) |
| httpbin.org | Static | 5 bugs (SEO, a11y) |
| BuggyTasks (test app) | Starlette | 15+ bugs (fake CRUD, auth bypass, broken dates) |

Zero false positives across all tested sites.

## Alternative: Standalone CLI

```bash
pip install argus-testing
playwright install chromium
export DEEPSEEK_API_KEY=sk-...
argus http://localhost:3000 --model deepseek/deepseek-chat -n 50
```

Supports 100+ models via [LiteLLM](https://github.com/BerriAI/litellm).

## Requirements

- Python 3.10+
- Chromium (auto-installed via `playwright install chromium`)

## License

MIT
