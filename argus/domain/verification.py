from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class VerificationSpec:
    kind: str
    expect: str = ""
    target_text: str = ""
    at_url: str = ""
    expect_status: int | None = None

    @classmethod
    def parse(cls, clause: Mapping[str, Any]) -> VerificationSpec:
        kind = clause.get("kind") or (
            "replay"
            if clause.get("replay")
            else "http_status"
            if clause.get("expect_status") is not None
            else "text"
        )
        if not isinstance(kind, str):
            raise ValueError("verification kind must be a string")
        if kind not in {"text", "http_status", "replay"}:
            return cls(kind)
        fields = {
            "expect": clause.get("expect") or "",
            "target_text": clause.get("target_text") or clause.get("target") or "",
            "at_url": clause.get("at_url") or clause.get("after_url") or "",
        }
        if not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("expect, target_text and at_url must be strings")
        fields = {key: value.strip() for key, value in fields.items()}
        fields["expect"] = fields["expect"].lower()
        status = clause.get("expect_status")
        if kind == "http_status":
            if isinstance(status, bool) or not isinstance(status, (int, str)):
                raise ValueError("expect_status must be an HTTP status integer")
            try:
                status = int(status)
            except ValueError as exc:
                raise ValueError("expect_status must be an HTTP status integer") from exc
            if not 100 <= status <= 599:
                raise ValueError("expect_status must be between 100 and 599")
            if fields["expect"] or fields["target_text"]:
                raise ValueError("use either expect_status or expect/target_text, not both")
        elif fields["expect"] not in {"present", "absent"} or not fields["target_text"]:
            raise ValueError("text verification needs expect present/absent and target_text")
        elif status is not None:
            raise ValueError("text/replay verification cannot include expect_status")
        return cls(kind=kind, expect_status=status, **fields)
