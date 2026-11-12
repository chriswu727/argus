"""Live verification (A), verify_persistence (B) and the exported test (C) agree.

Each fixture page has one true reading of its target text: present, absent or
inconclusive. For both claims (expect=present, expect=absent) every checker
must reach the same three-way verdict: reproduced, not_reproduced or
inconclusive. verify_persistence keeps two documented policies; a row that
relies on one names it in `b_policy`, which changes B's expected verdict
explicitly instead of skipping B.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

import argus.mcp_server as m
from argus.application.regression import check_entries
from argus.application.testgen import generate_test, run_exported_test
from argus.domain.verification import receipt_outcome

TARGET = "Buy groceries"
REPRODUCED, NOT_REPRODUCED, INCONCLUSIVE = "reproduced", "not_reproduced", "inconclusive"
FILLER = "<p>" + ("lorem ipsum dolor sit amet " * 260) + "</p>"
TASKS = "<h1>Tasks</h1><ul><li>Walk dog</li><li>Read book</li></ul>"
HIDDEN_ROW = (
    "<h1>Tasks</h1><ul><li>Walk dog<span style='display:none'> Buy groceries</span></li>"
    "<li>Read book</li></ul>"
)
SHADOW = (
    "<h1>Tasks</h1><x-row></x-row><script>customElements.define('x-row', class extends "
    "HTMLElement{connectedCallback(){this.attachShadow({mode:'open'}).innerHTML="
    "'<button>Buy groceries</button>'}})</script>"
)
HASH_APP = (
    "<h1>Profile</h1><p id='out'></p><button onclick=\"window.saved='Buy groceries';render()\">"
    "Save</button><script>function render(){document.getElementById('out').textContent="
    "window.saved||''}window.onhashchange=render;render()</script>"
)
ENGLISH_LOGIN = "<h1>Account</h1><p>Please sign in to continue</p>"
SHADOW_LOGIN = (
    "<h1>Account</h1><x-wall></x-wall><script>customElements.define('x-wall', class extends "
    "HTMLElement{connectedCallback(){this.attachShadow({mode:'open'}).innerHTML="
    "'<p>Please sign in to continue</p>'}})</script>"
)
GERMAN_LOGIN = (
    "<h2>Willkommen</h2><form><input name='u'><input type='password' name='p'>"
    "<button type='submit'>Anmelden</button></form>"
)
API_LIST = (
    "<h1>Tasks</h1><ul id='l'></ul><script>fetch('/api/items').then(r=>r.json()).then(d=>"
    "{document.getElementById('l').innerHTML=d.items.map(i=>'<li>'+i+'</li>').join('')})</script>"
)
REDIRECT = {
    "/t": (302, "", "/home"),
    "/home": (200, "<h1>Welcome</h1><p>Start here</p>", None),
}


class _Pages(http.server.BaseHTTPRequestHandler):
    pages: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/hang":
            time.sleep(20)
            return
        if path == "/api/items":
            body = json.dumps({"items": ["Real item", "Walk dog"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        status, body, location = type(self).pages.get(path, (200, "<p>home</p>", None))
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture(scope="module")
def sites():
    class Main(_Pages):
        pages: dict = {}

    class Other(_Pages):
        pages = {"/frame": (200, "<p>Buy groceries</p>", None)}

    main, base = _serve(Main)
    other, other_base = _serve(Other)
    try:
        yield Main, base, other_base
    finally:
        for server in (main, other):
            server.shutdown()
            server.server_close()


@dataclass(frozen=True)
class Case:
    pages: dict
    truth: str
    path: str = "/t"
    target: str = TARGET
    setup: tuple = ()
    mock: tuple = ()
    b_expect_status: int = 0
    b_policy: str = ""


def _claim_outcome(truth: str, expect: str) -> str:
    if truth == INCONCLUSIVE:
        return INCONCLUSIVE
    return REPRODUCED if truth == expect else NOT_REPRODUCED


# verify_persistence's documented policies, and the verdict each one gives it.
B_POLICIES = {
    # An observed presence is reported as conclusive on any page, error pages included.
    "presence_is_conclusive": lambda expect: _claim_outcome("present", expect),
    # A status other than expect_status is a conclusive mismatch, whatever the text says.
    "status_mismatch_is_conclusive": lambda expect: NOT_REPRODUCED,
}

CASES = {
    "plain_present": Case(
        {"/t": (200, "<h1>Tasks</h1><ul><li>Buy groceries</li></ul>", None)}, "present"
    ),
    "plain_absent": Case({"/t": (200, TASKS, None)}, "absent"),
    "beyond_5000_chars": Case(
        {"/t": (200, "<h1>Article</h1>" + FILLER + "<p>Buy groceries</p>", None)}, "present"
    ),
    "login_phrase_beyond_5000_chars": Case(
        {
            "/t": (
                200,
                "<h1>Article</h1><p>Buy groceries</p>"
                + FILLER
                + "<footer>Log in to reply</footer>",
                None,
            )
        },
        "present",
    ),
    "cjk_target": Case(
        {"/t": (200, "<h1>任务</h1><p>任务已完成</p>", None)}, "present", target="已完成"
    ),
    "iframe_only": Case(
        {"/t": (200, "<h1>Tasks</h1><iframe srcdoc='<p>Buy groceries</p>'></iframe>", None)},
        "present",
    ),
    "cross_origin_iframe": Case(
        {"/t": (200, "<h1>Tasks</h1><iframe src='{other}/frame'></iframe>", None)}, "present"
    ),
    "shadow_root_button": Case({"/t": (200, SHADOW, None)}, "present"),
    "hidden_text_in_visible_row": Case({"/t": (200, HIDDEN_ROW, None)}, "absent"),
    "hidden_input_value": Case(
        {
            "/t": (
                200,
                "<h1>Tasks</h1><form><input type='hidden' name='l' value='Buy groceries'></form>"
                "<p>Walk dog</p>",
                None,
            )
        },
        "absent",
    ),
    "visible_input_value": Case(
        {"/t": (200, "<h1>Edit</h1><input name='n' value='Buy groceries'>", None)}, "present"
    ),
    "hash_route_memory_state": Case(
        {"/app": (200, HASH_APP, None)}, "absent", path="/app#/profile", setup=("Save",)
    ),
    "login_wall_english": Case({"/t": (200, ENGLISH_LOGIN, None)}, INCONCLUSIVE),
    "login_wall_german": Case({"/t": (200, GERMAN_LOGIN, None)}, INCONCLUSIVE),
    "login_wall_in_shadow_root": Case({"/t": (200, SHADOW_LOGIN, None)}, INCONCLUSIVE),
    "redirect_elsewhere": Case(REDIRECT, INCONCLUSIVE),
    "http_500": Case({"/t": (500, "<h1>Application unavailable</h1>", None)}, INCONCLUSIVE),
    "http_500_showing_target": Case(
        {"/t": (500, "<h1>Error</h1><p>Buy groceries</p>", None)},
        INCONCLUSIVE,
        b_policy="presence_is_conclusive",
    ),
    "login_wall_unexpected_status": Case(
        {"/t": (200, ENGLISH_LOGIN, None)},
        INCONCLUSIVE,
        b_expect_status=404,
        b_policy="status_mismatch_is_conclusive",
    ),
    "never_settles": Case(
        {"/t": (200, "<h1>Tasks</h1><p>Loading…</p><script>fetch('/hang')</script>", None)},
        INCONCLUSIVE,
    ),
    "agent_mock_active": Case(
        {"/t": (200, API_LIST, None)},
        "absent",
        target="Mocked item",
        mock=("**/api/items", 200, json.dumps({"items": ["Mocked item", "Walk dog"]})),
    ),
}


def _fn(name):
    tool = getattr(m, name)
    return getattr(tool, "fn", tool)


def _pages(pages, other):
    return {
        path: (status, body.replace("{other}", other), loc)
        for path, (status, body, loc) in pages.items()
    }


def _a_verdict(receipt) -> str:
    outcome = receipt_outcome(receipt)
    return INCONCLUSIVE if outcome is None else REPRODUCED if outcome else NOT_REPRODUCED


def _b_verdict(check) -> str:
    matches = check["matches"]
    return INCONCLUSIVE if matches is None else REPRODUCED if matches else NOT_REPRODUCED


def _c_verdict(run) -> str:
    outcome = run["outcome"]
    if outcome == "fails_on_symptom":
        return REPRODUCED
    if outcome == "passes":
        return NOT_REPRODUCED
    if outcome == INCONCLUSIVE:
        return INCONCLUSIVE
    return f"{outcome}: {run.get('detail')}"


async def _start(url):
    try:
        await _fn("start_session")(url)
    except Exception as exc:
        pytest.skip(f"Chromium/session unavailable: {exc}")
    if m.runtime.session is None or m.runtime.session.browser is None:
        pytest.skip("Chromium/session unavailable")


async def _live(case, url, expect):
    await _start(url)
    try:
        if case.mock:
            await _fn("network_mock")(*case.mock)
            await _fn("navigate")(url)
        for label in case.setup:
            await _fn("click_what")(label)
        await _fn("record_bug")(
            "finding",
            "high",
            {"expected": "x", "actual": "y"},
            verify={"expect": expect, "target_text": case.target, "at_url": url},
        )
        receipt = m.runtime.session.bugs[-1].reproduction_receipt
        for label in case.setup:
            await _fn("click_what")(label)
        result = await _fn("verify_persistence")(
            expect, case.target, url, expect_status=case.b_expect_status
        )
        return receipt, result.data["verification"]
    finally:
        await _fn("end_session")()


async def _exported(case, url, expect, login_page, tmp_path):
    secrets: dict = {}
    source, notes = generate_test(
        finding_id="run:1",
        title="finding",
        expected="x",
        receipt={
            "expect": expect,
            "target_text": case.target,
            "at_url": url,
            "login_page": login_page,
        },
        journey=[],
        start_url=url,
        resolve_input=lambda action: None,
        filename="regression.py",
        secrets=secrets,
    )
    assert notes == []
    path = tmp_path / f"regression_{expect}.py"
    path.write_text(source, encoding="utf-8")
    return await run_exported_test(path, 120, secrets=secrets)


_VERDICTS: dict = {}


async def _verdicts(name, expect, sites, tmp_path):
    key = (name, expect)
    if key not in _VERDICTS:
        handler, base, other = sites
        case = CASES[name]
        handler.pages = _pages(case.pages, other)
        url = base + case.path
        receipt, check = await _live(case, url, expect)
        run = await _exported(case, url, expect, bool(receipt.get("login_page")), tmp_path)
        _VERDICTS[key] = {"A": _a_verdict(receipt), "B": _b_verdict(check), "C": _c_verdict(run)}
    return _VERDICTS[key]


def _params():
    for name in CASES:
        for expect in ("present", "absent"):
            for checker in ("A", "B", "C"):
                yield pytest.param(name, expect, checker, id=f"{name}-{expect}-{checker}")


def test_cases_name_real_policies():
    assert all(not case.b_policy or case.b_policy in B_POLICIES for case in CASES.values())


@pytest.mark.parametrize("name,expect,checker", list(_params()))
async def test_checkers_reach_the_same_verdict(name, expect, checker, sites, tmp_path):
    case = CASES[name]
    expected = _claim_outcome(case.truth, expect)
    if checker == "B" and case.b_policy:
        expected = B_POLICIES[case.b_policy](expect)
    assert (await _verdicts(name, expect, sites, tmp_path))[checker] == expected


# The page as recorded shows no target; afterwards the server changes it.
LATER = {
    "redirect_later": REDIRECT,
    "english_login_wall_later": {"/t": (200, ENGLISH_LOGIN, None)},
    "german_login_wall_later": {"/t": (200, GERMAN_LOGIN, None)},
}
_LATER_VERDICTS: dict = {}


async def _later_verdicts(change, sites, tmp_path):
    if change not in _LATER_VERDICTS:
        handler, base, _ = sites
        handler.pages = {"/t": (200, TASKS, None)}
        url = base + "/t"
        await _start(url)
        try:
            recorded = await _fn("record_bug")(
                "finding",
                "high",
                {"expected": "x", "actual": "y"},
                verify={"expect": "absent", "target_text": TARGET, "at_url": url},
            )
            exported = await _fn("export_test")(
                recorded.data["finding_id"], run=True, save_private_values=True
            )
            assert exported.data["run"]["outcome"] == "fails_on_symptom", exported
            handler.pages = LATER[change]
            spec = m.runtime.session.bugs[-1].verification_spec
            entries = [{"title": "f", "verify": spec}]
            regression = await check_entries(m.runtime.session, entries, base)
        finally:
            await _fn("end_session")()
        rerun = await run_exported_test(
            exported.data["path"],
            120,
            secrets={"ARGUS_VALUES_FILE": exported.data["private_values_path"]},
        )
        status = regression[0]["status"]
        _LATER_VERDICTS[change] = {
            "regression": {"STILL-PRESENT": REPRODUCED, "FIXED": NOT_REPRODUCED}.get(
                status, INCONCLUSIVE
            ),
            "exported": _c_verdict(rerun),
        }
    return _LATER_VERDICTS[change]


def _later_params():
    for change in LATER:
        for checker in ("regression", "exported"):
            yield pytest.param(change, checker, id=f"{change}-{checker}")


@pytest.mark.parametrize("change,checker", list(_later_params()))
async def test_regression_and_exported_file_agree_after_the_page_changes(
    change, checker, sites, tmp_path
):
    assert (await _later_verdicts(change, sites, tmp_path))[checker] == INCONCLUSIVE
