"""Turn a verified finding into a standalone regression test.

The test replays the session's recorded journey with Playwright and then asserts
the opposite of the verified symptom on a fresh load, so it fails while the bug
is present and passes once it is fixed. Locators are captured when each action
runs, from the element Argus resolved, so the test does not depend on Argus's
natural-language resolver.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from argus.domain.models import InteractiveElement
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


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return (slug or "finding")[:60]


def _path_or_url(url: str, origin: str) -> str:
    parsed = urlparse(url)
    if f"{parsed.scheme}://{parsed.netloc}" == origin:
        relative = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        # Keep the fragment: a hash-routed app (#/tasks) shows another screen.
        return relative + (f"#{parsed.fragment}" if parsed.fragment else "")
    return url


_HEADER = '''"""Regression test exported by Argus from finding {finding_id}.

{title}

Argus verified this symptom on a fresh load ({runs}): {symptom}.
Expected behavior: {expected}

The test replays the recorded journey and asserts the opposite of the symptom,
so it fails while the bug is present and passes once the bug is fixed.
Run it with `python {filename}` or pytest, using a Python that has Playwright
and Chromium (the interpreter that runs Argus has both). Set ARGUS_BASE_URL to
test another origin.{notes}
"""

import os
import re
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("ARGUS_BASE_URL", {origin})
SYMPTOM_MARKER = "ARGUS_SYMPTOM_PRESENT"


def _url(path):
    return urljoin(BASE_URL, path)


{row_helper}def _matches(text, haystack):
    """Argus's word-boundary rule on whitespace-normalised text."""
    haystack = " ".join(haystack.lower().split())
    needle = " ".join(text.lower().split())
    before = r"(?<!\\w)" if re.match(r"\\w", needle) else ""
    after = r"(?!\\w)" if re.search(r"\\w$", needle) else ""
    return re.search(before + re.escape(needle) + after, haystack) is not None


def _present(text, page, max_scrolls=20):
    """Text and field values, scrolling (bounded) like Argus's fresh-load check
    so a row a virtualized or lazy list renders further down is still found."""
    page.wait_for_load_state("networkidle")
    last_y = None
    for scrolls in range(max_scrolls + 1):
        values = page.eval_on_selector_all("input, textarea", "els => els.map(e => e.value)")
        if _matches(text, page.inner_text("body") + " " + " ".join(values)):
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


def {test_name}():
    with sync_playwright() as playwright:
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
) -> tuple[str, List[str]]:
    """Return (source, notes). `resolve_input(action)` yields a typed value.

    Sensitive values are never written to the file. When `secrets` is given it
    is filled with each environment variable name and its value, so a run from
    this session can pass them to the test without storing them."""
    target = receipt["target_text"]
    expect = receipt["expect"]
    at_url = receipt.get("at_url") or start_url
    parsed = urlparse(start_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    notes: List[str] = []
    env_inputs: List[str] = []
    lines = [f"page.goto(_url({_q(_path_or_url(start_url, origin))}))"]

    for number, action in enumerate(journey, 1):
        tool = action.get("tool")
        locator = action.get("locator")
        value = action.get("value")
        if action.get("accept_dialog"):
            lines.append('accept_dialog["pending"] = True')
        if tool == "navigate" and value:
            lines.append(f"page.goto(_url({_q(_path_or_url(str(value), origin))}))")
        elif tool == "double_click" and str(value or "").startswith("text:"):
            lines.append(f"page.get_by_text({_q(str(value)[5:])}, exact=True).dblclick()")
        elif tool in ("click_what", "double_click", "hover_what") and locator:
            method = {"click_what": "click", "double_click": "dblclick", "hover_what": "hover"}
            lines.append(f"{locator}.{method[tool]}()")
        elif tool in ("type_into", "select_into") and locator:
            if action.get("sensitive"):
                name = f"ARGUS_INPUT_{len(env_inputs) + 1}"
                env_inputs.append(f"{name}: {action.get('description', '')}")
                if secrets is not None:
                    secrets[name] = resolve_input(action) or ""
                text = f"os.environ[{_q(name)}]"
            else:
                text = _q(resolve_input(action) or "")
            method = "fill" if tool == "type_into" else "select_option"
            lines.append(f"{locator}.{method}({text})")
        elif tool == "set_checked" and locator:
            lines.append(f"{locator}.set_checked({str(value).lower() == 'true'})")
        elif tool == "press_key":
            key = str(action.get("key") or "").strip()
            if key and locator:
                lines.append(f"{locator}.press({_q(key)})")
            elif key:
                lines.append(f"page.keyboard.press({_q(key)})")
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
            what = action.get("description", "")
            notes.append(
                f"step {number}: upload_file {what!r} (attach the files with set_input_files)"
            )
            lines.append(f"# TODO(argus): step {number}: upload_file {what!r}")
        elif tool == "capsule_restore":
            name = action.get("description", "")
            notes.append(
                f"step {number}: the session restored capsule {name!r} (cookies and storage); "
                "establish that state here, for example by logging in"
            )
            lines.append(f"# TODO(argus): step {number}: establish the state of capsule {name!r}")
        else:
            notes.append(f"step {number}: {tool} {action.get('description', '')!r} not exported")
            lines.append(
                f"# TODO(argus): replay step {number} by hand: {tool} {action.get('description', '')!r}"
            )
        if tool in ("click_what", "double_click", "press_key", "set_checked", "go_back"):
            lines.append('page.wait_for_load_state("networkidle")')
        if action.get("accept_dialog"):
            lines.append('accept_dialog["pending"] = False')

    # Leave the page first so the check is a fresh load even when only the hash
    # route matches the current URL.
    lines.append('page.goto("about:blank")')
    lines.append(f"page.goto(_url({_q(_path_or_url(at_url, origin))}))")
    if expect == "present":
        lines.append(
            f"assert not _present({_q(target)}, page), "
            f"SYMPTOM_MARKER + {_q(f': {target!r} is still shown')}"
        )
        symptom = f"{target!r} is present at {at_url}"
    else:
        lines.append(
            f"assert _present({_q(target)}, page), "
            f"SYMPTOM_MARKER + {_q(f': {target!r} is missing')}"
        )
        symptom = f"{target!r} is absent at {at_url}"

    note_text = ""
    if env_inputs:
        note_text += "\n\nSet these environment variables (sensitive inputs are not stored):\n"
        note_text += "\n".join(f"  {item}" for item in env_inputs)
    if notes:
        note_text += "\n\nSteps that need manual translation:\n" + "\n".join(
            f"  {note}" for note in notes
        )
    test_name = f"test_{_slug(title)}"
    header = _HEADER.format(
        finding_id=finding_id,
        title=_doc(title),
        runs=receipt.get("runs", "verified"),
        symptom=_doc(symptom),
        expected=_doc(expected or "the symptom does not occur"),
        filename=filename,
        notes=_doc(note_text),
        origin=_q(origin),
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
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(secrets or {})},
        start_new_session=os.name == "posix",
    )
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
    stdout = out.decode(errors="replace")
    stderr = err.decode(errors="replace")
    tail = (stderr or stdout).strip().splitlines()
    last = tail[-1][:300] if tail else ""
    if proc.returncode == 0:
        return {"outcome": "passes", "detail": "the symptom did not appear"}
    if "ARGUS_SYMPTOM_PRESENT" in stderr:
        return {"outcome": "fails_on_symptom", "detail": last}
    return {"outcome": "errored", "detail": last}
