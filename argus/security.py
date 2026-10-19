from __future__ import annotations

import re


def export_trace(actions: list[dict]) -> list[dict]:
    result = []
    for action in actions:
        item = {"tool": action.get("tool"), "description": _redact(action.get("description") or "")}
        if action.get("value_ref") or action.get("tool") in {
            "type_into",
            "paste_into",
            "select_into",
            "type_at",
            "screen_type_into",
        }:
            item["requires_input"] = True
            item["value"] = None
        else:
            value = action.get("value")
            item["value"] = _redact(value) if isinstance(value, str) else value
        result.append(item)
    return result


_SECRET_KEY = (
    r"(?:[\w-]*(?:password|passwd|secret|token|jwt|bearer|auth|api[_-]?key|"
    r"access[_-]?key|session|sid|csrf|xsrf|credential|cookie)[\w-]*)"
)


_RE_JSON_STR = re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*)"[^"]*"', re.IGNORECASE)


_RE_JSON_BARE = re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*)(?!["{\[])([^\s,}\]]+)', re.IGNORECASE)


_RE_FORM = re.compile(r"(?i)\b(" + _SECRET_KEY + r")=([^&\s]+)")


_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+")


_RE_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")


_SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-xsrf-token",
}


def _redact(text: str) -> str:
    """Mask credentials in a free-text/JSON/form blob before it's surfaced.

    Layered because secrets arrive in many shapes: a generic JWT pattern, JSON
    string + bare values under secret-named keys, form-encoded pairs, and bare
    Bearer tokens. Not a guarantee against every exotic encoding, but it kills
    the common JWT/token/cookie/password leaks.
    """
    if not text:
        return text
    text = _RE_JWT.sub("[redacted-jwt]", text)
    text = _RE_JSON_STR.sub(r'\1"[redacted]"', text)
    text = _RE_JSON_BARE.sub(r"\1[redacted]", text)
    text = _RE_FORM.sub(r"\1=[redacted]", text)
    text = _RE_BEARER.sub(r"\1[redacted]", text)
    return text


def _redact_headers(headers: dict) -> dict:
    """Mask whole values of credential-bearing headers; redact the rest in place."""
    out = {}
    for k, v in (headers or {}).items():
        out[k] = "[redacted]" if k.lower() in _SENSITIVE_HEADERS else _redact(str(v))
    return out
