from __future__ import annotations

from typing import Awaitable, Callable

from argus.domain.verification import VerificationSpec

from .session import Session

Verifier = Callable[[Session, dict, list[dict]], Awaitable[dict]]


class VerificationRegistry:
    def __init__(self, strategies: dict[str, Verifier]):
        self._strategies = dict(strategies)

    def register(self, name: str, verifier: Verifier) -> None:
        if name in self._strategies:
            raise ValueError(f"verifier already registered: {name}")
        self._strategies[name] = verifier

    async def verify(
        self,
        session: Session,
        clause: dict,
        actions: list[dict] | None = None,
        *,
        screenshot_label: str | None = None,
        replay_start: str | None = None,
    ) -> dict:
        try:
            spec = VerificationSpec.parse(clause)
        except ValueError as exc:
            return {"attempted": False, "reason": str(exc)}
        verifier = self._strategies.get(spec.kind)
        if verifier is None:
            return {"attempted": False, "reason": f"unsupported verification kind: {spec.kind}"}
        # Underscore keys are set only here; never accept them from the caller.
        request = {key: value for key, value in clause.items() if not key.startswith("_")}
        if screenshot_label and spec.kind in {"text", "http_status"}:
            request["_screenshot_label"] = "".join(
                char if char.isalnum() else "_" for char in screenshot_label
            )[:50]
        if replay_start and spec.kind == "replay":
            # The page the recorded journey began on; replay must start there
            # so going back and the first navigation follow the same history.
            request["_replay_start_url"] = replay_start
        return await verifier(session, request, actions or [])
