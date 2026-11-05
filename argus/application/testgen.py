"""Turn a verified finding into a standalone regression test.

The test replays the session's recorded journey with Playwright and then asserts
the opposite of the verified symptom on a fresh load, so it fails while the bug
is present and passes once it is fixed. Locators are captured when each action
runs, from the element Argus resolved, so the test does not depend on Argus's
natural-language resolver.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from argus.domain.models import InteractiveElement
from argus.domain.verification import LOGIN_WALL_PHRASES
from argus.resolver import kind_of
from argus.security import _RE_SECRET_KEY

# Same containers extraction uses for an element's row (parent_context).
_ROW_SELECTOR = 'li, tr, .card, .list-item, [class*="item"], [class*="row"]'

_ROLES = {
    "button": "button",
    "link": "link",
    "checkbox": "checkbox",
    "radio": "radio",
    "switch": "switch",
    "tab": "tab",
    "menuitem": "menuitem",
    "option": "option",
    "select": "combobox",
}


# Explicit roles Playwright's get_by_role can target for the extracted widgets.
_KNOWN_ROLES = set(_ROLES.values()) | {
    "textbox",
    "slider",
    "spinbutton",
    "gridcell",
    "menuitemcheckbox",
    "menuitemradio",
}


def _effective_role(el: InteractiveElement) -> Optional[str]:
    """The accessible role Playwright sees: an explicit role wins over the tag
    (<button role="tab"> is a tab), and an unknown explicit role gets none."""
    explicit = (el.role or "").strip().lower()
    if explicit:
        return explicit if explicit in _KNOWN_ROLES else None
    return _ROLES.get(kind_of(el))


_UNNAMED_INPUT = {"checkbox": 'input[type="checkbox"]', "radio": 'input[type="radio"]'}


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _single_locator(el: InteractiveElement) -> Optional[str]:
    """A Playwright locator for `el`, before any duplicate disambiguation."""
    if el.id:
        return f"page.locator({_q(f'[id={json.dumps(el.id)}]')})"
    tag = el.tag
    if tag in ("input", "textarea", "select"):
        if el.placeholder:
            return f"page.get_by_placeholder({_q(el.placeholder)}, exact=True)"
        if el.name and (el.type or "").lower() not in ("checkbox", "radio"):
            return f"page.locator({_q(f'{tag}[name={json.dumps(el.name)}]')})"
        kind = (el.type or "").lower()
        if el.css_path and kind in ("checkbox", "radio") and el.parent_context:
            # Unnamed: locate it by input type inside its row (see locator_expr),
            # not by a DOM path that breaks when the markup or order changes.
            # CSS, unlike get_by_role, also finds role="switch" or aria-hidden.
            return f"page.locator({_q(_UNNAMED_INPUT[kind])})"
        if el.css_path:
            return f"page.locator({_q(el.css_path)})"
    role, name = _role_and_name(el)
    if role and name:
        return f"page.get_by_role({_q(role)}, name={_q(name)}, exact=True)"
    if el.name:
        return f"page.locator({_q(f'{tag}[name={json.dumps(el.name)}]')})"
    if el.aria_label:
        return f"page.get_by_label({_q(el.aria_label)}, exact=True)"
    if el.text and tag in ("a", "button"):
        return f"page.get_by_text({_q(' '.join(el.text.split()))}, exact=True)"
    return None


_UNNAMED_BASES = {f"page.locator({_q(css)})" for css in _UNNAMED_INPUT.values()}


def locator_expr(el: InteractiveElement, elements: List[InteractiveElement]) -> Optional[str]:
    elements = [other for other in elements if other.frame == el.frame]
    expression = _locator_in_frame(el, elements)
    if expression is None or not el.frame:
        return expression
    scope = "page"
    for selector in el.frame.split(" >>> "):
        scope += f".frame_locator({_q(selector)})"
    if expression.startswith("_in_row(page,"):
        return expression.replace("_in_row(page,", f"_in_row({scope},", 1)
    return scope + expression[len("page") :]


def _locator_in_frame(el: InteractiveElement, elements: List[InteractiveElement]) -> Optional[str]:
    """Locator expression for `el`, scoped to its row when the bare locator
    would also match another listed element (N identical Delete buttons)."""
    base = _single_locator(el)
    if base is None:
        return None
    unnamed = base in _UNNAMED_BASES
    # Every listed control the emitted locator could also match, including
    # ones Argus locates differently (an id-bearing "Delete" in another row).
    twins = [other for other in elements if other is not el and _overlaps(el, other, base)]
    row = " ".join((el.parent_context or "").split())
    if unnamed and any(" ".join((o.parent_context or "").split()) == row for o in twins):
        # The role alone would match another control in the same row.
        return f"page.locator({_q(el.css_path or '')})"
    if not twins and not unnamed:
        return base
    key = _row_key(el.parent_context or "", [o.parent_context or "" for o in twins])
    if key:
        inner = base[len("page.") :]
        return f"_in_row(page, {_q(key)}, lambda scope: scope.{inner})"
    if unnamed and el.css_path:
        return f"page.locator({_q(el.css_path)})"
    position = sum(1 for o in elements[: elements.index(el)] if _overlaps(el, o, base))
    return f"{base}.nth({position})"


def _role_and_name(el: InteractiveElement) -> tuple:
    name = (el.aria_label or el.text or "").strip()
    if (el.type or "").lower() in ("submit", "button") and not name:
        name = (el.value or "").strip()
    return _effective_role(el), " ".join(name.split())


def _overlaps(el: InteractiveElement, other: InteractiveElement, base: str) -> bool:
    """Whether `base`, emitted for `el`, would also match `other` on the page."""
    if _single_locator(other) == base:
        return True
    if base in _UNNAMED_BASES:
        # input[type=...] matches every input of that type, however located.
        return other.tag == "input" and (other.type or "").lower() == (el.type or "").lower()
    if not base.startswith("page.get_by_role("):
        return False
    role, name = _role_and_name(el)
    other_role, other_name = _role_and_name(other)
    if role is None or role != other_role:
        return False
    # A role without a name matches every control with that role.
    return "name=" not in base or name == other_name


def _row_key(row: str, others: List[str]) -> Optional[str]:
    """The fewest leading words of `row` (at least two when it has them) that
    no other row contains. Rows usually start with their title and end with
    changing details ("5 days ago"), so the key stays stable over time."""
    words = row.split()
    for count in range(min(2, len(words)), len(words) + 1):
        key = " ".join(words[:count])[:60]
        if not any(key.lower() in " ".join(o.split()).lower() for o in others):
            return key
    return None


def is_sensitive(el: InteractiveElement, description: str) -> bool:
    if (el.type or "").lower() == "password":
        return True
    faces = " ".join(filter(None, [el.name, el.id, el.aria_label, el.placeholder, description]))
    return bool(_RE_SECRET_KEY.search(faces))


def _doc(text: str) -> str:
    """Text safe inside the generated module's triple-quoted docstring."""
    return text.replace("\\", "\\\\").replace('"""', "'''")


_HEADER = '''"""Regression test exported by Argus.

{title}

Argus verified this symptom on a fresh load ({runs}): {symptom}.
Expected behavior: {expected}

The test replays the recorded journey and asserts the opposite of the symptom,
so it fails while the bug is present and passes once the bug is fixed.
Run this file with Python or pytest, with Playwright and Chromium installed
(the interpreter that runs Argus has both). Set ARGUS_BASE_URL to
test another origin. Supply values through environment variables or a private
JSON file selected with ARGUS_VALUES_FILE; environment variables take precedence.
Never commit or publish the values file.{notes}
"""

import json
import os
import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

LOGIN_WALL_PHRASES = {login_wall_phrases}
REQUIRED_ENV = {required_env}
EXPORT_ID = {export_id}


def _values():
    values = {{}}
    if os.environ.get("ARGUS_VALUES_FILE"):
        try:
            with open(os.environ["ARGUS_VALUES_FILE"], encoding="utf-8") as stream:
                saved = json.load(stream)
        except Exception:
            raise _ExportFailure("ARGUS_VALUES_FILE could not be read as a values object") from None
        if not isinstance(saved, dict) or not isinstance(saved.get("values"), dict):
            raise _ExportFailure("ARGUS_VALUES_FILE could not be read as a values object")
        if saved.get("export_id") != EXPORT_ID:
            raise _ExportFailure("ARGUS_VALUES_FILE belongs to another export")
        values = {{name: saved["values"][name] for name in REQUIRED_ENV if name in saved["values"]}}
    overrides = {{name: os.environ[name] for name in REQUIRED_ENV if name in os.environ}}
    if overrides and os.environ.get("ARGUS_EXPORT_ID") != EXPORT_ID:
        raise _ExportFailure("ARGUS_EXPORT_ID must match this exported test")
    values.update(overrides)
    if any(not isinstance(value, str) for value in values.values()):
        raise _ExportFailure("ARGUS_VALUES_FILE values must be strings")
    for name in REQUIRED_ENV:
        if name not in values:
            raise _ExportFailure(f"Missing required environment variable: {{name}}")
    parsed = urlparse(values["ARGUS_URL_1"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _ExportFailure("ARGUS_URL_1 must be an absolute http(s) URL")
    return values


def _origin(values):
    parsed = urlparse(values["ARGUS_URL_1"])
    return parsed.scheme + "://" + parsed.netloc.rsplit("@", 1)[-1]


def _url(path, values):
    if os.environ.get("ARGUS_BASE_URL"):
        parsed = urlparse(path)
        origin = parsed.scheme + "://" + parsed.netloc.rsplit("@", 1)[-1]
        if origin == _origin(values):
            path = parsed._replace(scheme="", netloc="", path=parsed.path or "/").geturl()
    return urljoin(os.environ.get("ARGUS_BASE_URL", _origin(values)), path)


{row_helper}def _matches(text, haystack):
    """Argus's word-boundary rule on whitespace-normalised text."""
    haystack = " ".join(haystack.lower().split())
    needle = " ".join(text.lower().split())
    before = r"(?<!\\w)" if re.match(r"\\w", needle) else ""
    after = r"(?!\\w)" if re.search(r"\\w$", needle) else ""
    return re.search(before + re.escape(needle) + after, haystack) is not None


def _check_page(page, response):
    if response is not None and response.status >= 400:
        raise _ExportFailure("fresh-load check returned an HTTP error")
    page.wait_for_load_state("networkidle")
    text = page.inner_text("body").lower()
    login_heading = page.get_by_role(
        "heading", name=re.compile(r"^(sign in|log in|login)$", re.IGNORECASE)
    )
    password_login = page.locator('input[type="password"]:visible').count() and login_heading.count()
    if password_login or any(phrase in text for phrase in LOGIN_WALL_PHRASES):
        raise _ExportFailure("fresh-load check hit a login wall")


def _present(text, page, max_scrolls=20):
    """Text and field values, scrolling (bounded) like Argus's fresh-load check
    so a row a virtualized or lazy list renders further down is still found."""
    page.wait_for_load_state("networkidle")
    last_y = None
    for scrolls in range(max_scrolls + 1):
        for frame in page.frames:
            if frame.is_detached():
                continue
            body = frame.locator("body")
            if not body.count():
                continue
            values = frame.eval_on_selector_all("input, textarea", "els => els.map(e => e.value)")
            if _matches(text, body.inner_text() + " " + " ".join(values)):
                return True
        y, at_bottom = page.evaluate(
            "() => [window.scrollY, "
            "window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2]"
        )
        if scrolls == max_scrolls or at_bottom or y == last_y:
            return False
        last_y = y
        page.evaluate("() => window.scrollBy(0, 1000)")
        page.wait_for_timeout(400)
    return False


def _replay():
{preflight}    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
{dialog_setup}        try:
'''

_ROW_HELPER = '''ROW_SELECTOR = {row_selector}


def _in_row(page, text, find):
    """`find(scope)` within the row showing `text`: the innermost row holding
    the text and the control, not a list container that also matches."""
    rows = page.locator(ROW_SELECTOR).filter(has_text=text).filter(has=find(page))
    return find(rows.filter(has_not=rows)).first


'''

_DIALOG_SETUP = """        # Accept a dialog only during a step that accepted it in the session;
        # any other dialog is dismissed, as it was there.
        accept_dialog = {"pending": False}

        def _on_dialog(dialog):
            if accept_dialog["pending"]:
                accept_dialog["pending"] = False
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", _on_dialog)
"""

_FOOTER = """        finally:
            browser.close()


class _ExportFailure(RuntimeError):
    pass


class _ExportSymptom(AssertionError):
    pass


def {test_name}():
    try:
        _replay()
    except _ExportSymptom:
        raise AssertionError("ARGUS_SYMPTOM_PRESENT") from None
    except _ExportFailure as exc:
        raise RuntimeError(str(exc)) from None
    except Exception:
        raise RuntimeError("ARGUS_EXECUTION_ERROR: replay or fresh-load check failed; raw browser details withheld") from None


if __name__ == "__main__":
    {test_name}()
    print("PASS: the symptom is gone")
"""


def generate_test(
    *,
    finding_id: str,
    title: str,
    expected: str,
    receipt: dict,
    journey: List[dict],
    start_url: str,
    resolve_input,
    filename: str,
    secrets: Optional[dict] = None,
    required_env: Optional[dict] = None,
    export_id: Optional[str] = None,
) -> tuple[str, List[str]]:
    """Return source and manual steps without embedding application strings.

    Values stay in the session and are passed through the child environment for
    an immediate run. Only fixed purpose labels are exposed in required_env.
    """
    export_id = export_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", export_id):
        raise ValueError("export_id must be a UUID hex string")
    if secrets is not None:
        secrets["ARGUS_EXPORT_ID"] = export_id
    target = receipt["target_text"]
    expect = receipt["expect"]
    at_url = receipt.get("at_url") or start_url
    notes: List[str] = []
    bindings: dict[str, dict[str, str]] = {}
    purposes: dict[str, str] = {}

    def binding(value: str, category: str, purpose: str) -> str:
        values = bindings.setdefault(category, {})
        if value not in values:
            name = f"ARGUS_{category}_{len(values) + 1}"
            values[value] = name
            purposes[name] = purpose
            if secrets is not None:
                secrets[name] = value
        else:
            name = values[value]
            if purpose not in purposes[name].split("; "):
                purposes[name] += "; " + purpose
        return f"values[{_q(name)}]"

    def private_locator(expression: str, step: int) -> str:
        class Parameters(ast.NodeTransformer):
            def visit_Constant(self, node):
                if isinstance(node.value, str):
                    return ast.parse(
                        binding(node.value, "LOCATOR", f"step {step} locator text"), mode="eval"
                    ).body
                return node

        return ast.unparse(Parameters().visit(ast.parse(expression, mode="eval")))

    initial = binding(start_url, "URL", "initial navigation URL")
    lines = [f"page.goto(_url({initial}, values))"]

    for number, action in enumerate(journey, 1):
        tool = action.get("tool")
        locator = action.get("locator")
        if locator:
            locator = private_locator(locator, number)
        value = action.get("value")
        if action.get("accept_dialog"):
            lines.append('accept_dialog["pending"] = True')
        if tool == "navigate" and value:
            url = binding(str(value), "URL", f"step {number} navigation URL")
            lines.append(f"page.goto(_url({url}, values))")
        elif tool in ("double_click", "hover_what") and str(value or "").startswith("text:"):
            method = "dblclick" if tool == "double_click" else "hover"
            # Visible matches only, as when recorded: a hidden copy of the same
            # text (another layout) would otherwise make the locator ambiguous.
            text = binding(str(value)[5:], "TEXT", f"step {number} visible text")
            lines.append(f"page.get_by_text({text}, exact=True).filter(visible=True).{method}()")
        elif tool in ("click_what", "double_click", "hover_what") and locator:
            method = {"click_what": "click", "double_click": "dblclick", "hover_what": "hover"}
            lines.append(f"{locator}.{method[tool]}()")
        elif tool in ("type_into", "select_into") and locator:
            resolved = resolve_input(action)
            if resolved is None:
                notes.append(f"step {number}: input value unavailable; supply it before running")
            text = binding(resolved or "", "INPUT", f"step {number} input value")
            method = "fill" if tool == "type_into" else "select_option"
            lines.append(f"{locator}.{method}({text})")
        elif tool == "set_checked" and locator:
            lines.append(f"{locator}.set_checked({str(value).lower() == 'true'})")
        elif tool == "press_key":
            key = str(action.get("key") or "").strip()
            key_expr = binding(key, "KEY", f"step {number} key") if key else ""
            if key and locator:
                lines.append(f"{locator}.press({key_expr})")
            elif key:
                lines.append(f"page.keyboard.press({key_expr})")
            else:
                notes.append(f"step {number}: press_key without a recorded key")
                lines.append(f"# TODO(argus): step {number} press_key needs a key")
        elif tool == "go_back":
            lines.append("page.go_back()")
        elif tool == "scroll_down":
            lines.append('page.evaluate("() => window.scrollBy(0, 500)")')
            lines.append("page.wait_for_timeout(500)")
        elif tool == "resize" and re.fullmatch(r"\d+x\d+", str(action.get("description"))):
            width, height = str(action["description"]).split("x")
            lines.append(f'page.set_viewport_size({{"width": {width}, "height": {height}}})')
        elif tool == "upload_file":
            notes.append(f"step {number}: upload_file (attach the files with set_input_files)")
            lines.append(f"# TODO(argus): step {number}: upload_file")
        elif tool == "capsule_restore":
            notes.append(f"step {number}: establish restored session state before continuing")
            lines.append(f"# TODO(argus): step {number}: establish restored session state")
        else:
            notes.append(f"step {number}: unsupported action; translate it before running")
            lines.append(f"# TODO(argus): replay step {number} by hand")
        if tool in ("click_what", "double_click", "press_key", "set_checked", "go_back"):
            lines.append('page.wait_for_load_state("networkidle")')
        if action.get("accept_dialog"):
            lines.append('accept_dialog["pending"] = False')

    # Leave the page first so the check is a fresh load even when only the hash
    # route matches the current URL.
    lines.append('page.goto("about:blank")')
    final_url = binding(at_url, "URL", "fresh-load verification URL")
    lines.append(f"response = page.goto(_url({final_url}, values))")
    lines.append("_check_page(page, response)")
    target_expr = binding(target, "TEXT", "fresh-load assertion text")
    condition = "" if expect == "present" else "not "
    lines.append(f"if {condition}_present({target_expr}, page):")
    lines.append("    raise _ExportSymptom()")

    note_text = "\n\nRequired environment variables (values are not stored):\n"
    note_text += "\n".join(f"  {name}: {purpose}" for name, purpose in purposes.items())
    note_text += "\n  ARGUS_EXPORT_ID: " + export_id + " (required for environment overrides)"
    if required_env is not None:
        required_env.update(purposes)
        required_env["ARGUS_EXPORT_ID"] = "export identity: " + export_id
    if notes:
        note_text += "\n\nSteps that need manual translation:\n" + "\n".join(notes)
    preflight = ""
    if notes:
        preflight = '    raise _ExportFailure("ARGUS_INCOMPLETE_TEST: translate every manual step, then remove this guard before running")\n'
    preflight += "    values = _values()\n"
    test_name = "test_argus_finding"
    header = _HEADER.format(
        title="Application details are supplied through environment variables.",
        runs="verified",
        symptom="the recorded text-presence symptom",
        expected="the symptom does not occur",
        preflight=preflight,
        notes=_doc(note_text),
        login_wall_phrases=json.dumps(LOGIN_WALL_PHRASES),
        required_env=repr(list(purposes)),
        export_id=_q(export_id),
        row_helper=(
            _ROW_HELPER.format(row_selector=_q(_ROW_SELECTOR))
            if any("_in_row(" in line for line in lines)
            else ""
        ),
        dialog_setup=_DIALOG_SETUP if any("accept_dialog[" in line for line in lines) else "",
        test_name=test_name,
    )
    body = "".join(f"            {line}\n" for line in lines)
    return header + body + _FOOTER.format(test_name=test_name), notes


def _kill_tree(proc) -> None:
    """Stop the test and everything it started (Playwright driver, Chromium)."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def run_exported_test(path: Path, timeout: float, secrets: Optional[dict] = None) -> dict:
    """Run the exported test once and classify the outcome.

    The test runs in its own process group. On timeout or cancellation the
    whole group is killed before returning, so its browser cannot keep
    repeating the journey's writes after the operation ends."""
    environment = {**os.environ, **(secrets or {})}
    if secrets and "ARGUS_EXPORT_ID" in secrets:
        environment.pop("ARGUS_VALUES_FILE", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError):
        return {
            "outcome": "errored",
            "detail": "could not start the exported test; check runtime and environment values",
        }
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        return {"outcome": "errored", "detail": f"timed out after {timeout:.0f}s"}
    except BaseException:
        _kill_tree(proc)
        await asyncio.shield(proc.wait())
        raise
    stderr = err.decode(errors="replace")
    if proc.returncode == 0:
        return {"outcome": "passes", "detail": "the symptom did not appear"}
    last = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    if last.startswith("RuntimeError: ARGUS_INCOMPLETE_TEST:"):
        return {"outcome": "incomplete", "detail": "Translate every manual step before running."}
    if last == "AssertionError: ARGUS_SYMPTOM_PRESENT":
        return {"outcome": "fails_on_symptom", "detail": "the verified symptom is still present"}
    if re.fullmatch(
        r"RuntimeError: Missing required environment variable: ARGUS_[A-Z]+_[0-9]+", last
    ):
        return {"outcome": "errored", "detail": last.removeprefix("RuntimeError: ")}
    for diagnostic in (
        "fresh-load check returned an HTTP error",
        "fresh-load check hit a login wall",
        "ARGUS_URL_1 must be an absolute http(s) URL",
        "ARGUS_VALUES_FILE could not be read as a values object",
        "ARGUS_VALUES_FILE values must be strings",
        "ARGUS_VALUES_FILE belongs to another export",
        "ARGUS_EXPORT_ID must match this exported test",
    ):
        if last == "RuntimeError: " + diagnostic:
            return {"outcome": "errored", "detail": diagnostic}
    return {
        "outcome": "errored",
        "detail": "replay or fresh-load check failed; raw subprocess output withheld",
    }
