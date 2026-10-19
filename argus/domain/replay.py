from __future__ import annotations

from typing import TypedDict
from uuid import uuid4

from typing_extensions import NotRequired


class ReplayAction(TypedDict):
    tool: str
    description: str
    value: NotRequired[str | None]
    value_ref: NotRequired[str]


class InputVault:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def store(self, value: str) -> str:
        reference = uuid4().hex
        self._values[reference] = value
        return reference

    def resolve(self, actions: list[dict]) -> list[dict]:
        resolved = []
        for action in actions:
            item = dict(action)
            if reference := item.get("value_ref"):
                if reference not in self._values:
                    raise ValueError(
                        "replay input is unavailable; supply it again in the current session"
                    )
                item["value"] = self._values[reference]
                del item["value_ref"]
            elif item.get("requires_input"):
                raise ValueError(
                    "exported replay requires fresh input; redacted values cannot be replayed"
                )
            resolved.append(item)
        return resolved

    def clear(self) -> None:
        self._values.clear()
