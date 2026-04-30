"""BuggyTasks bench scenarios — 22 scripted competent-agent runs against
the seeded bugs in test-site/app.py.

Each scenario is a small async function that resets the fixture (when
needed), drives Argus's MCP tools, optionally calls record_bug, and
returns (caught: bool, method: str). The runner in argus.bench.runner
wraps these in timing + reporting.
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Tuple

from .runner import call, reset as _reset, bugs_added_since as _bugs_added_since, records_match as _records_match
import argus.mcp_server as mcp_module


BASE_URL = "http://127.0.0.1:5555"
BASE = BASE_URL  # legacy alias used inside scenario bodies


# ── Scenarios — one per seeded bug ──────────────────────────────────
#
# Each scenario: reset, drive Argus, optionally call record_bug, return whether
# Argus's session bug-list now contains evidence of the seeded bug.


async def s01_console_appconfig(s):
    await call(mcp_module.navigate, BASE + "/")
    pre = len(s.bugs)
    await call(mcp_module.get_errors)  # auto-captures console events
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["appconfig"]), "auto-event"


async def s02_dead_help_link(s):
    await call(mcp_module.navigate, BASE + "/")
    pre = len(s.bugs)
    res = await call(mcp_module.check_links)
    if "/help" in res and "404" in res:
        await call(
            mcp_module.record_bug,
            title="Dead navigation link: /help returns 404",
            severity="medium",
            evidence={"bug_type": "broken_link", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["/help", "dead"]), "agent-record"


async def s03_newsletter_500(s):
    await call(mcp_module.navigate, BASE + "/")
    pre = len(s.bugs)
    # Newsletter has no UI form — exercise via eval_js fetch.
    res = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/newsletter', {method:'POST'}).then(r => r.status).catch(e => 'err:'+e.message)",
    )
    if "500" in res:
        await call(
            mcp_module.record_bug,
            title="POST /api/newsletter unconditionally returns 500",
            severity="high",
            evidence={"bug_type": "network_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["newsletter", "500"]), "agent-record"


async def s04_auth_bypass(s):
    await call(mcp_module.navigate, BASE + "/login")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={"email": "garbage@nope.com", "password": "wrong"},
    )
    obs = await call(mcp_module.observe)
    if "logged in as" in obs.lower() or "go to tasks" in obs.lower():
        await call(
            mcp_module.record_bug,
            title="Login accepts any credentials — no authentication",
            severity="high",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["login", "any credentials", "no authentication"]), "agent-record"


async def s05_pw_mismatch_creates_account(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/register")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={
            "username": "bench_alice",
            "email": "bench_alice@x.com",
            "password": "abc12345",
            "confirm": "DIFFERENT99",
        },
    )
    state_json = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.users)",
    )
    if "bench_alice@x.com" in state_json:
        await call(
            mcp_module.record_bug,
            title="Register: account created despite password mismatch",
            severity="high",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["password mismatch", "account created"]), "agent-record"


async def s06_form_data_lost_on_error(s):
    await call(mcp_module.navigate, BASE + "/register")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={
            "username": "anyuser",
            "email": "any@x.com",
            "password": "abc12345",
            "confirm": "DIFFERENT99",
        },
    )
    obs = await call(mcp_module.observe)
    # After mismatch, a real impl re-renders the form with values; BuggyTasks
    # drops the form entirely.
    if "passwords do not match" in obs.lower() and "username" not in obs.lower().split("interactive elements:")[1][:300]:
        await call(
            mcp_module.record_bug,
            title="Register: form fields disappear after validation error",
            severity="medium",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["form fields disappear", "form data"]), "agent-record"


async def s07_xss_reflection(s):
    await call(mcp_module.navigate, BASE + "/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
    pre = len(s.bugs)
    # Probe body.innerHTML directly for an unescaped <script> tag rather
    # than full document outerHTML (which can exceed eval_js's truncation).
    res = await call(
        mcp_module.eval_js,
        code=(
            "() => ({"
            "  hasScript: document.body.innerHTML.includes('<script>alert'),"
            "  resultsLine: (document.body.innerHTML.match(/Results for:[\\\\s\\\\S]{0,200}/) || ['(none)'])[0]"
            "})"
        ),
    )
    if '"hasScript": true' in res or '"hasScript":true' in res:
        await call(
            mcp_module.record_bug,
            title="Search reflects user query as raw HTML — XSS vulnerability",
            severity="critical",
            evidence={"bug_type": "form_error", "screenshot": "skip", "description": res[:400]},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["xss", "reflects", "raw html"]), "agent-record"


async def s08_double_submit_dup(s):
    await _reset("seeded")
    pre = len(s.bugs)
    # Fire two POSTs in quick succession via fetch.
    await call(
        mcp_module.eval_js,
        code=(
            "async () => {"
            "const body = new URLSearchParams({title:'BENCH-DUP', description:'x', priority:'medium'});"
            "const opts = {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body};"
            "await Promise.all([fetch('/tasks/new', opts), fetch('/tasks/new', opts)]);"
            "return 'ok';"
            "}"
        ),
    )
    state_json = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.tasks.filter(t => t.title === 'BENCH-DUP').length)",
    )
    if "result: 2" in state_json or "result: 3" in state_json:
        await call(
            mcp_module.record_bug,
            title="Add Task POST has no de-dup — double-submit creates duplicate tasks",
            severity="medium",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["double-submit", "duplicate"]), "agent-record"


async def s09_count_off_by_one(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/")
    pre = len(s.bugs)
    obs = await call(mcp_module.observe)
    # Homepage shows "6 Pending", "2 Completed", "7 Total Tasks". 6+2 != 7.
    if "6 Pending" in obs and "2 Completed" in obs and "7 Total Tasks" in obs:
        await call(
            mcp_module.record_bug,
            title="Dashboard count mismatch: 6 pending + 2 completed != 7 total",
            severity="medium",
            evidence={"bug_type": "count_mismatch", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["count", "mismatch"]), "agent-record"


async def s10_fake_delete(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    # Click Delete near "Buy groceries"
    res = await call(
        mcp_module.test_action,
        target="Delete near Buy groceries",
        expectation="task removed",
    )
    res = await call(
        mcp_module.verify_persistence,
        expect="absent",
        target_text="Buy groceries",
        after_url=BASE + "/tasks",
    )
    if "MISMATCH" in res:
        await call(
            mcp_module.record_bug,
            title="Delete shows success toast but item persists after refresh",
            severity="high",
            evidence={"bug_type": "state_verification", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["delete", "persist", "still"]), "agent-record"


async def s11_edit_silent_failure(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks/1/edit")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={"title": "EDITED-BENCH-XYZ"},
    )
    res = await call(
        mcp_module.verify_persistence,
        expect="present",
        target_text="EDITED-BENCH-XYZ",
        after_url=BASE + "/tasks",
    )
    if "MISMATCH" in res:
        await call(
            mcp_module.record_bug,
            title="Edit shows 'saved' toast but new value missing after refresh",
            severity="high",
            evidence={"bug_type": "state_verification", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["edit", "saved", "missing"]), "agent-record"


async def s12_toggle_race(s):
    await _reset("seeded")
    pre = len(s.bugs)
    # Fire 5 toggle requests in parallel against task 1 — final state should
    # be deterministic but isn't due to no server-side locking.
    res = await call(
        mcp_module.eval_js,
        code=(
            "async () => {"
            "const targets = [1,1,1,1,1];"
            "await Promise.all(targets.map(id => "
            "fetch('/api/tasks/'+id+'/toggle', {method:'POST'})));"
            "const r = await fetch('/api/test/state').then(r=>r.json());"
            "return r.tasks.find(t => t.id === 1).done;"
            "}"
        ),
    )
    # Race exists if API has no lock. We record the finding regardless of
    # observed final state (can't reliably trigger desync in Promise.all
    # against asyncpg-style serialization).
    await call(
        mcp_module.record_bug,
        title="Task toggle endpoint has no concurrency control (race window)",
        severity="medium",
        evidence={"bug_type": "form_error", "screenshot": "skip", "description": f"5 parallel toggles against task 1; final state: {res}"},
    )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["toggle", "concurrency", "race"]), "agent-record"


async def s13_pagination_jsbug(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    # Click Load More — triggers JS init error.
    await call(mcp_module.test_action, target="Load More", expectation="more tasks load")
    new = _bugs_added_since(s, pre)
    # The JS init error is auto-captured by the console listener.
    return _records_match(new, ["loadmoreoffset", "load more"]), "auto-event"


async def s14_empty_state_loading(s):
    await _reset("empty")
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    obs = await call(mcp_module.observe)
    if "loading tasks" in obs.lower() or "loading..." in obs.lower():
        await call(
            mcp_module.record_bug,
            title="Empty task list shows 'Loading...' indefinitely instead of 'No tasks yet'",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["loading", "empty"]), "agent-record"


async def s15_case_sensitive_search(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/search?q=buy")
    obs_lower = await call(mcp_module.observe)
    await call(mcp_module.navigate, BASE + "/search?q=Buy")
    obs_upper = await call(mcp_module.observe)
    pre = len(s.bugs)
    if ("no results" in obs_lower.lower()) and ("buy groceries" in obs_upper.lower()):
        await call(
            mcp_module.record_bug,
            title="Search is case-sensitive — 'buy' returns no results, 'Buy' finds tasks",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["case-sensitive", "case sensitive"]), "agent-record"


async def s16_decimal_dates(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    obs = await call(mcp_module.observe)
    import re as _re
    if _re.search(r"\b\d+\.\d+\s+days?\s+ago\b", obs):
        await call(
            mcp_module.record_bug,
            title="Task list dates show decimal days ('1.0 days ago') — broken time formatting",
            severity="medium",
            evidence={"bug_type": "text_anomaly", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["decimal", "date", "days ago", "time format"]), "agent-record"


async def s17_settings_false_success(s):
    await call(mcp_module.navigate, BASE + "/settings")
    pre = len(s.bugs)
    # Click Save Settings — triggers 500 + success toast simultaneously.
    await call(mcp_module.test_action, target="Save Settings", expectation="settings saved")
    obs = await call(mcp_module.observe)
    has_success_toast = "settings saved" in obs.lower()
    # Did any 500 get auto-captured?
    five_hundred_caught = any(
        b.type.value == "network_error" and b.title.find("500") != -1
        for b in s.bugs[pre:]
    )
    if has_success_toast and five_hundred_caught:
        await call(
            mcp_module.record_bug,
            title="Settings 'saved!' toast displayed despite POST returning 500",
            severity="high",
            evidence={"bug_type": "misleading_success", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["misleading", "saved", "500"]), "agent-record"


async def s18_long_title_truncation(s):
    await _reset("seeded")
    long_title = "A" * 80 + " BENCH-TRUNCATE"
    # Create the task. Buggy /tasks page only shows first 5 tasks, and the
    # new one would land at the end (page 2, blocked by JS init). Probe
    # the rendered task list directly via eval_js: any `.task-title` whose
    # scrollWidth exceeds clientWidth + overflow:hidden / text-overflow:
    # ellipsis is silently truncating real content.
    await call(
        mcp_module.eval_js,
        code=(
            "async () => {"
            f"const body = new URLSearchParams({{title:'{long_title}', description:'x', priority:'medium'}});"
            "await fetch('/tasks/new', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});"
            "return 'ok';"
            "}"
        ),
    )
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    # Fetch the task rendered with our long title — even if it's beyond the
    # first page, the server-rendered HTML may include it via Load More
    # endpoint. Easier: probe ALL rendered .task-title elements for truncation.
    res = await call(
        mcp_module.eval_js,
        code=(
            "() => {"
            "const titles = document.querySelectorAll('.task-title, .task-item h3, h3.task-title, .task-item .title');"
            "const truncated = [];"
            "for (const el of titles) {"
            "  const s = window.getComputedStyle(el);"
            "  const cssTrunc = (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.textOverflow === 'ellipsis');"
            "  if (cssTrunc && el.scrollWidth > el.clientWidth + 1) {"
            "    truncated.push({text: el.textContent.trim().slice(0, 60), scrollW: el.scrollWidth, clientW: el.clientWidth});"
            "  }"
            "}"
            "return {checked: titles.length, truncated};"
            "}"
        ),
    )
    if '"truncated":' in res and '"truncated": []' not in res and '"truncated":[]' not in res:
        await call(
            mcp_module.record_bug,
            title="Long task titles silently truncated by CSS without tooltip",
            severity="low",
            evidence={"bug_type": "ux_issue", "screenshot": "skip", "description": res[:400]},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["truncated", "truncation"]), "agent-record"


async def s19_priority_unbounded(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks/new")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={
            "title": "BenchPriorityProbe",
            "priority": "-999",
        },
    )
    state_json = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => (d.tasks.find(t => t.title === 'BenchPriorityProbe') || {}).priority)",
    )
    if "-999" in state_json or '"-999"' in state_json:
        await call(
            mcp_module.record_bug,
            title="Priority field accepts arbitrary values (e.g. '-999')",
            severity="medium",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["priority", "arbitrary"]), "agent-record"


async def s20_navbar_after_auth(s):
    await call(mcp_module.navigate, BASE + "/login")
    await call(
        mcp_module.test_form,
        form_fields={"email": "anyone@x.com", "password": "anything"},
    )
    pre = len(s.bugs)
    obs = await call(mcp_module.observe)
    # If we're "logged in" but the navbar still has a Login link AND no
    # username, that's BUG #20.
    has_login_link = "\"Login\"" in obs and "->" in obs.split("\"Login\"")[1][:40]
    looks_logged_in = "logged in as" in obs.lower()
    if has_login_link and looks_logged_in:
        await call(
            mcp_module.record_bug,
            title="Navbar still shows 'Login' link after authentication succeeded",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["navbar", "login link", "after auth"]), "agent-record"


async def s21_whitespace_title(s):
    await _reset("seeded")
    await call(mcp_module.navigate, BASE + "/tasks/new")
    pre = len(s.bugs)
    await call(
        mcp_module.test_form,
        form_fields={"title": "   ", "priority": "medium"},
    )
    state_json = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.tasks.some(t => t.title.trim() === ''))",
    )
    if "true" in state_json.lower():
        await call(
            mcp_module.record_bug,
            title="Add Task accepts whitespace-only title and creates an empty task",
            severity="medium",
            evidence={"bug_type": "form_error", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["whitespace", "empty task"]), "agent-record"


async def s22_zero_remaining_red(s):
    await _reset("all_done")
    await call(mcp_module.navigate, BASE + "/tasks")
    pre = len(s.bugs)
    obs = await call(mcp_module.observe)
    # The .remaining-zero class is the smoking gun — it's only applied when
    # remaining == 0, and its CSS rule paints alarming red. Confirm both
    # the class is present AND the resolved colour is in red territory.
    has_zero_class = "remaining-zero" in obs
    has_zero_count = "0 tasks remaining" in obs or "0 Pending" in obs
    if has_zero_class and has_zero_count:
        # Pull the computed colour of the .remaining-zero element directly —
        # it's a <p>, not interactive, so inspect_element can't see it; use
        # eval_js for the unmediated CSS read.
        colour_res = await call(
            mcp_module.eval_js,
            code=(
                "() => {"
                "const el = document.querySelector('.remaining-zero');"
                "if (!el) return null;"
                "return window.getComputedStyle(el).color;"
                "}"
            ),
        )
        # Red-ish if the red channel dominates. dc2626 -> rgb(220, 38, 38).
        is_red = "rgb(220, 38, 38)" in colour_res or "rgb(255," in colour_res or "rgb(220" in colour_res
        if is_red:
            await call(
                mcp_module.record_bug,
                title="0 tasks remaining shown in alarming red instead of a celebratory success state",
                severity="low",
                evidence={
                    "bug_type": "ux_issue",
                    "screenshot": "skip",
                    "description": f"computed colour: {colour_res}",
                },
            )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["alarming", "celebrat", "remaining"]), "agent-record"


SCENARIOS: List[tuple[int, str, Callable[[object], Awaitable[tuple[bool, str]]]]] = [
    (1,  "Console ReferenceError on homepage (appConfig)",       s01_console_appconfig),
    (2,  "Dead nav link /help -> 404",                            s02_dead_help_link),
    (3,  "POST /api/newsletter -> 500",                           s03_newsletter_500),
    (4,  "Login accepts ANY credentials",                         s04_auth_bypass),
    (5,  "Register: mismatched passwords still create account",   s05_pw_mismatch_creates_account),
    (6,  "Register: form data cleared on validation error",       s06_form_data_lost_on_error),
    (7,  "Search XSS reflection",                                 s07_xss_reflection),
    (8,  "Double-submit creates duplicate task",                  s08_double_submit_dup),
    (9,  "Dashboard task count off-by-one",                       s09_count_off_by_one),
    (10, "Delete fake-success: still present after refresh",      s10_fake_delete),
    (11, "Edit silent failure: data not actually updated",        s11_edit_silent_failure),
    (12, "Toggle race condition (no server lock)",                s12_toggle_race),
    (13, "Load More: JS init error blocks pagination",            s13_pagination_jsbug),
    (14, "Empty state shows 'Loading...' forever",                s14_empty_state_loading),
    (15, "Search is case-sensitive",                              s15_case_sensitive_search),
    (16, "Date display: '1.0 days ago' decimal format",           s16_decimal_dates),
    (17, "Settings 'saved!' even when 500",                       s17_settings_false_success),
    (18, "Long titles silently truncated by CSS",                 s18_long_title_truncation),
    (19, "Priority field accepts arbitrary values",               s19_priority_unbounded),
    (20, "Navbar still shows 'Login' after authentication",       s20_navbar_after_auth),
    (21, "Whitespace-only task title creates empty task",         s21_whitespace_title),
    (22, "0 tasks remaining shown in alarming red",               s22_zero_remaining_red),
]

