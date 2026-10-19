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
        self, session: Session, clause: dict, actions: list[dict] | None = None
    ) -> dict:
        try:
            spec = VerificationSpec.parse(clause)
        except ValueError as exc:
            return {"attempted": False, "reason": str(exc)}
        verifier = self._strategies.get(spec.kind)
        if verifier is None:
            return {"attempted": False, "reason": f"unsupported verification kind: {spec.kind}"}
        return await verifier(session, clause, actions or [])
