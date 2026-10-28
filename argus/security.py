from __future__ import annotations

import json
import re
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from urllib.parse import unquote


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
        if action.get("accept_dialog"):
            # The journey accepted a confirmation here; replaying without it
            # would follow a different path.
            item["accept_dialog"] = True
        result.append(item)
    return result


_SECRET_KEY = (
    r"(?:[\w-]*(?:password|passwd|secret|token|jwt|bearer|auth|api[_-]?key|"
    r"access[_-]?key|session|sid|csrf|xsrf|credential|cookie)[\w-]*)"
)


_RE_SECRET_KEY = re.compile(_SECRET_KEY, re.IGNORECASE)


_RE_JSON_STR = re.compile(r'("' + _SECRET_KEY + r'"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE)
_RE_JSON_KEY = re.compile(r'("(?:[^"\\]|\\.)*")\s*:')


_RE_FORM = re.compile(r"([\w%.\-]+)=([^&\s#\"<>]+)")
_RE_URL_AUTH = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)


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
    if "content-disposition:" in text.lower() and "form-data;" in text.lower():
        return "[redacted multipart body]"
    if text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            sanitized = redacted_copy(parsed)
            return text if sanitized == parsed else json.dumps(sanitized, ensure_ascii=False)
        except (ValueError, RecursionError):
            for key in _RE_JSON_KEY.findall(text):
                try:
                    if _RE_SECRET_KEY.search(json.loads(key)):
                        return "[redacted malformed structured data]"
                except ValueError:
                    continue
    text = _RE_JWT.sub("[redacted-jwt]", text)
    text = _RE_JSON_STR.sub(r'\1"[redacted]"', text)
    text = _RE_FORM.sub(
        lambda m: f"{m[1]}=[redacted]" if _RE_SECRET_KEY.search(unquote(m[1])) else m[0],
        text,
    )
    text = _RE_URL_AUTH.sub(r"\1[redacted]@", text)
    text = _RE_BEARER.sub(r"\1[redacted]", text)
    return text


def _redact_headers(headers: dict) -> dict:
    """Mask whole values of credential-bearing headers; redact the rest in place."""
    out = {}
    for k, v in (headers or {}).items():
        out[k] = (
            "[redacted]"
            if k.lower() in _SENSITIVE_HEADERS or _RE_SECRET_KEY.search(k)
            else _redact(str(v))
        )
    return out


def redacted_copy(value):
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return _redact(value)
    if is_dataclass(value) and not isinstance(value, type):
        return replace(
            value, **{f.name: redacted_copy(getattr(value, f.name)) for f in fields(value)}
        )
    if isinstance(value, dict):
        return {
            k: "[redacted]" if _RE_SECRET_KEY.search(str(k)) else redacted_copy(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redacted_copy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redacted_copy(item) for item in value)
    return value


def redact_body(text: str, content_type: str = "") -> str:
    if "multipart/" in content_type.lower():
        return "[redacted multipart body]"
    return _redact(text)
