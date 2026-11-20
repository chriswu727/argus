"""Packaged text is UTF-8 whatever the platform's default encoding is.

Windows defaults to the ANSI code page (cp1252, or cp936/cp932/cp949/cp950 on
East Asian systems), not UTF-8. The browser scripts contain non-ASCII text, so
reading them with the default encoding garbled them on cp1252 and made
`import argus.mcp_server` raise UnicodeDecodeError on East Asian Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.resources import files

from argus import browser

_SCRIPTS = files("argus.adapters.scripts")


def _utf8(name: str) -> str:
    # Text mode also translates newlines (a Windows checkout may have CRLF).
    text = _SCRIPTS.joinpath(name).read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def test_loaded_browser_scripts_match_their_utf8_source():
    row = _utf8("row_text_js.js").strip()
    expected = {
        "_ROW_TEXT_JS": row,
        "_EXTRACT_ELEMENTS_JS": _utf8("extract_elements_js.js").replace(
            "ARGUS_ROW_TEXT_JS", row, 1
        ),
        "_EXTRACT_PAGE_CONTENT_JS": _utf8("extract_page_content_js.js").replace(
            "ARGUS_ROW_TEXT_JS", row, 1
        ),
        "_INSPECT_ELEMENT_JS": _utf8("inspect_element_js.js"),
        "_FIND_VISIBLE_ELEMENT_JS": _utf8("find_visible_element_js.js"),
        "_CHECK_LAYOUT_JS": _utf8("check_layout_js.js"),
        "_COLLECT_TEXT_JS": _utf8("collect_text_js.js"),
        "_SCAN_STEP_JS": _utf8("scan_step_js.js"),
        "_REF_ENGINE_JS": _utf8("ref_engine_js.js"),
        "_EXTRACT_PERFORMANCE_JS": _utf8("extract_performance_js.js"),
    }
    for name, source in expected.items():
        assert getattr(browser, name) == source, name
    # Keeps the comparison meaningful: a garbled read differs only where the
    # source has non-ASCII text, such as the iframe marker shown to agents.
    assert "— contents not readable here" in browser._EXTRACT_ELEMENTS_JS


def test_server_imports_when_the_default_encoding_is_not_utf8():
    # POSIX: the C locale without coercion or UTF-8 mode gives ASCII.
    # Windows: LC_ALL is ignored; -X utf8=0 keeps the ANSI code page.
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env.update(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    code = (
        "import locale, argus, argus.mcp_server; "
        "print(locale.getpreferredencoding(False), argus.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
