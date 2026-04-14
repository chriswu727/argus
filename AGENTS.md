# Argus — Agent Guide

This file helps AI agents (Claude Code, Cursor, etc.) use Argus effectively via MCP.

## Setup

```bash
pip install argus-testing
playwright install chromium
claude mcp add argus -- argus-mcp
```

## Recommended Workflow

### Quick scan (passive, read-only)
```
start_session(url) → get_errors() → check_links() → check_performance() → end_session()
```

### Interactive testing (tests forms and actions)
```
start_session(url) → get_page_state() → test_form(...) → test_action(...) → end_session()
```

### Full site audit
```
start_session(url) → crawl_site(max_pages=20) → end_session()
```

### CRUD verification
```
start_session(url) → test_crud(create_url, list_url, item_data) → end_session()
```

## Tool Selection Guide

| Goal | Use |
|------|-----|
| Scan a page for all issues | `get_errors()` — runs 12 passive detectors automatically |
| Check all links on a page | `check_links()` |
| Measure page speed | `check_performance()` |
| Test a form submission | `test_form(form_fields, submit_text, expected_result)` |
| Click a button and verify what changed | `test_action(element_index, action_description)` |
| Test create/edit/delete cycle | `test_crud(create_url, list_url, item_data)` |
| Verify a delete/edit persisted | `verify_action(action_type, target_text, verify_url)` |
| Scan entire site automatically | `crawl_site(max_pages)` |

## Compound Tools (preferred)

Use these instead of low-level click/type/navigate — they do more per call:

- **test_action(index, desc)** — Click + auto-capture before/after state + diff + detect bugs. Returns what changed.
- **test_form(fields, submit_text, expected_result)** — Fill form + submit + verify. Fields matched by name/placeholder.
- **test_crud(create_url, list_url, item_data)** — Full create → verify → edit → verify → delete → verify cycle.

## What get_errors() Detects

Every call to `get_errors()` runs these detectors automatically:
- Console errors and exceptions
- HTTP 4xx/5xx responses
- Broken dates ("1.52 days ago"), NaN, eternal "Loading..."
- Count mismatches (displayed number vs actual items)
- Broken images
- Missing meta description, OG tags, heading hierarchy
- Unlabeled form inputs, missing alt text, no lang attribute
- Mixed content (HTTP on HTTPS)
- Misleading success (toast says "Saved!" but server returned 500)

## Tips

- Always call `get_errors()` after navigating to a new page
- Use `test_form(..., expected_result="validation_error")` when testing with invalid data
- After delete/edit, use `verify_action()` to confirm persistence
- `crawl_site()` is the most thorough option — use it for full audits
- `get_page_state()` returns page text, toasts, counts, and CSS indicators — read these for context
