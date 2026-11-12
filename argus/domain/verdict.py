"""Whether a verified symptom is present, absent, or cannot be judged.

Live verification, verify_persistence and exported regression tests all decide
with these functions. export_test copies this file verbatim into every exported
test, so it uses only the standard library, never imports from argus, and has
no `from __future__` import (that must be a file's first statement). Its
functions take plain data: per-frame facts from the in-page text collector,
status codes, URLs and flags.

A frame's facts are a dict with the collector's keys: "text" (visible text,
open shadow roots included), "fields" (values of visible form fields),
"headings", "buttons" (labels of visible button controls), "password"
(a visible password field exists) and "truncated". The main frame comes first.
"""

import re
from urllib.parse import urlsplit

VERDICT_VERSION = "1"

# Scripts written without spaces between words have no word boundary to test:
# "已完成" inside "任务已完成" is a real occurrence, not a mid-word match.
UNSPACED = (
    "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff"  # Thai, Lao, Myanmar, Khmer
    "\u3040-\u30ff\u31f0-\u31ff"  # Hiragana, Katakana
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK ideographs
    "\uac00-\ud7af"  # Hangul syllables (particles attach without a space)
    "\U00020000-\U0003134f"  # CJK extensions
)
WORD_CHAR = f"[^\\W{UNSPACED}]"

LOGIN_WALL_PHRASES = (
    "please log in",
    "please sign in",
    "log in to",
    "sign in to",
    "session expired",
    "session has expired",
    "you have been logged out",
    "you are logged out",
    "log in to continue",
    "sign in to continue",
)
# A phrase deep in a long page ("Log in to reply" under an article) is not a wall.
LOGIN_SCAN_CHARS = 5000
LOGIN_HEADINGS = ("sign in", "log in", "login")
LOGIN_WORDS = (
    "sign in",
    "signin",
    "log in",
    "login",
    "log on",
    "anmelden",
    "einloggen",
    "connexion",
    "se connecter",
    "iniciar sesión",
    "iniciar sesion",
    "acceder",
    "accedi",
    "entrar",
    "войти",
    "вход",
    "登录",
    "登入",
    "登錄",
    "ログイン",
    "로그인",
)

MAX_SCROLLS = 20
SCROLL_STEP = 1000

REASON_CODES = ("http_error", "login_wall", "login_form", "moved", "truncated", "unsettled")


def token_present(needle: str, haystack: str) -> bool:
    """Whitespace-normalised, word-boundary containment.

    A bare substring scan lets a claimed symptom match incidentally ('cat' in
    'category', 'Delete' in 'Deleted'), which would certify a non-bug. The
    needle must sit on word boundaries, so only a token-level occurrence counts.
    """
    needle = " ".join(needle.lower().split())
    haystack = " ".join(haystack.lower().split())
    if not needle:
        return False
    # A boundary matters only on a word-character edge of the needle: "-50%"
    # printed straight after a price ("$89.99-50%") is a real occurrence.
    before = f"(?<!{WORD_CHAR})" if re.match(WORD_CHAR, needle) else ""
    after = f"(?!{WORD_CHAR})" if re.search(WORD_CHAR + "$", needle) else ""
    return re.search(before + re.escape(needle) + after, haystack) is not None


def frames_present(target: str, frames: list) -> bool:
    """The target is in some frame's visible text or a visible field's value."""
    for frame in frames:
        if token_present(target, frame.get("text") or ""):
            return True
        if any(token_present(target, value or "") for value in frame.get("fields") or ()):
            return True
    return False


def main_frame(frames: list) -> dict:
    return frames[0] if frames else {}


def wall_phrase(text: str) -> bool:
    preview = (text or "")[:LOGIN_SCAN_CHARS].lower()
    return any(phrase in preview for phrase in LOGIN_WALL_PHRASES)


def looks_logged_out(frame: dict) -> bool:
    heading = any(
        str(heading).strip().lower() in LOGIN_HEADINGS for heading in frame.get("headings") or ()
    )
    return (heading and bool(frame.get("password"))) or wall_phrase(frame.get("text") or "")


def is_login_page(frame: dict) -> bool:
    """A sign-in page by its own heading, not a page saying the session ended."""
    return looks_logged_out(frame) and not wall_phrase(frame.get("text") or "")


def login_form(frame: dict) -> bool:
    """A password form whose heading or button names a login action.

    Wider than looks_logged_out (any wording, common UI languages), so it only
    decides whether an ABSENT symptom can be trusted, never a present one.
    """
    if not frame.get("password"):
        return False
    labels = [*(frame.get("headings") or ()), *(frame.get("buttons") or ())]
    return any(token_present(word, str(label)) for label in labels for word in LOGIN_WORDS)


def _page(url: str) -> tuple:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), parts.query


def same_page(requested: str, landed: str) -> bool:
    return _page(requested) == _page(landed) and (
        urlsplit(requested).fragment == urlsplit(landed).fragment
    )


def moved_from(requested: str, landed: str) -> bool:
    """True when the load ended on a different page than the one requested."""
    if not landed:
        return False
    want, got = urlsplit(requested), urlsplit(landed)
    if (want.scheme, want.netloc.lower(), want.path.rstrip("/")) != (
        got.scheme,
        got.netloc.lower(),
        got.path.rstrip("/"),
    ):
        return True
    # A hash route is part of the page identity; a plain anchor is not.
    return want.fragment.startswith(("/", "!")) and want.fragment != got.fragment


def status_observation(
    expected: int, direct: int | None, final: int | None, moved: bool, login: bool
) -> tuple:
    """Compare one fresh load against an expected HTTP status.

    Returns (matched, reason); matched None means the load cannot tell whether
    the recorded response is still served.
    """
    if final is None or direct is None:
        return None, "clean load returned no HTTP response — can't compare the status"
    if expected >= 400:
        if expected in (direct, final):
            return True, None
        if final >= 400:
            return None, (
                f"clean load returned HTTP {final}, not the recorded {expected} — "
                "a different failure doesn't show the recorded one is fixed"
            )
        if moved or login:
            return None, (
                "clean load was redirected or hit a login page — "
                f"can't tell whether HTTP {expected} is still returned"
            )
        return False, None
    if direct == expected and not moved:
        if login:
            return (
                None,
                f"clean load returned HTTP {direct} for a login page — not the recorded page",
            )
        return True, None
    if moved or direct in (401, 403, 407, 429) or direct >= 500:
        return None, (
            f"clean load answered HTTP {direct}"
            + (" and ended on another page" if moved else "")
            + f" — access or availability, not necessarily a fix of the recorded HTTP {expected}"
        )
    return False, None


def load_gate(load: dict, status_expected: bool = False) -> str | None:
    """Why this load cannot show the symptom either way, before looking for it.

    `load` holds one fresh load's facts: "status" (final HTTP status or None),
    "requested" and "landed" URLs, "frames", "settled", "login_page" (the
    checked page is itself a sign-in page) and "login_page_recorded" (that was
    established when the finding was recorded). Returns a reason code or None.
    """
    status = load.get("status")
    if status is not None and status >= 400 and not status_expected:
        return "http_error"
    main = main_frame(load.get("frames") or [])
    if looks_logged_out(main) and (
        not load.get("login_page")
        or not same_page(load.get("requested") or "", load.get("landed") or "")
        or wall_phrase(main.get("text") or "")
    ):
        return "login_wall"
    return None


def text_observation(
    load: dict, present: bool, present_is_conclusive: bool = False, status_expected: bool = False
) -> tuple:
    """One load of a text check: (True present / False absent / None, reason code).

    Absence is trusted only on the requested page, fully read, settled and not
    behind a login form. present_is_conclusive trusts an observed presence on
    any page; status_expected accepts an HTTP error status the caller expected.
    """
    if present and present_is_conclusive:
        return True, None
    reason = load_gate(load, status_expected)
    if reason:
        return None, reason
    if present:
        return True, None
    frames = load.get("frames") or []
    main = main_frame(frames)
    if (looks_logged_out(main) or login_form(main)) and not load.get("login_page_recorded"):
        return None, "login_form"
    if moved_from(load.get("requested") or "", load.get("landed") or ""):
        return None, "moved"
    if any(frame.get("truncated") for frame in frames):
        return None, "truncated"
    if not load.get("settled", True):
        return None, "unsettled"
    return False, None


def reason_text(code: str, status: int | None = None, landed: str = "") -> str:
    if code == "http_error":
        return f"clean load returned HTTP {status or 'error'} — can't attribute the symptom"
    if code == "login_wall":
        return "clean load hit a login wall — the session may have expired"
    if code == "login_form":
        return "clean load hit a login wall — absence cannot confirm an application symptom"
    if code == "moved":
        where = (urlsplit(landed).path or "/") if landed else "another page"
        return (
            f"clean load ended on {where} instead of the requested page — absence there "
            "cannot confirm an application symptom; verify at the page that actually shows "
            "the content"
        )
    if code == "truncated":
        return "page text exceeds the inspection limit — absence is not reliable evidence"
    if code == "unsettled":
        return "clean load did not settle — absence is not reliable evidence"
    return code


def persistence_check(expect: str, present: bool, load: dict, expect_status: int = 0) -> dict:
    """verify_persistence's verdict for one load.

    Two policies differ from a recorded receipt: an observed presence is always
    conclusive, and a status other than expect_status is a conclusive mismatch.
    """
    status = load.get("status")
    text_matches = present == (expect == "present")
    status_matches = (status == expect_status) if expect_status else None
    reason = None
    if expect_status and status is None:
        conclusive = False
    elif status_matches is False:
        conclusive = True
    else:
        observed, reason = text_observation(
            load, present, present_is_conclusive=True, status_expected=bool(status_matches)
        )
        conclusive = observed is not None
    return {
        "conclusive": conclusive,
        "matches": (text_matches and status_matches is not False) if conclusive else None,
        "text_matches": text_matches,
        "status_matches": status_matches,
        "reason": reason,
    }


def scroll_exhausted(scrolls: int, at_bottom: bool, y, last_y) -> bool:
    """Stop scrolling for a lazily rendered row: budget spent, bottom, or stalled."""
    return scrolls >= MAX_SCROLLS or bool(at_bottom) or (y is not None and y == last_y)


def receipt_verdict(observations: list, expect: str) -> dict:
    """Verdict from present/absent observations against the claim.

    reproduced = every run matched the claim; flaky = some but not all did.
    """
    matches = [(p and expect == "present") or (not p and expect == "absent") for p in observations]
    hits = sum(1 for m in matches if m)
    return {
        "attempted": True,
        "reproduced": bool(matches) and all(matches),
        "flaky": 0 < hits < len(matches),
        "runs": f"{hits}/{len(matches)}",
        "expect": expect,
    }


def receipt_outcome(receipt) -> bool | None:
    """True reproduced, False not reproduced, None inconclusive.

    A symptom seen on some fresh loads but not all is neither confirmed nor
    refuted, whatever the receipt's own `reproduced` flag says.
    """
    if not receipt or receipt.get("flaky"):
        return None
    return receipt.get("reproduced")
