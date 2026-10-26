from __future__ import annotations

from copy import deepcopy

from .overhead import json_bytes

FIELDS = frozenset(
    {"page", "visible_text", "targets", "counts", "feedback", "coverage", "omitted", "follow_up"}
)
RESET_OPERATIONS = frozenset(
    {"observe", "start_session", "navigate", "go_back", "verify_persistence"}
)


class ObservationSnapshots:
    """One redacted observation per server; missing bases require a fresh observe."""

    def __init__(self, max_bytes: int = 256 * 1024, max_deltas: int = 16):
        self.max_bytes = max_bytes
        self.max_deltas = max_deltas
        self.sequence = 0
        self.clear()

    def clear(self) -> None:
        self.base: dict = {}
        self.base_id: str | None = None
        self.identity: tuple | None = None
        self.deltas = 0

    def encode(
        self, data: dict, operation: str, run_id: str | None, *, error: bool = False
    ) -> dict:
        observation = data.get("observation", data)
        if (
            error
            or not run_id
            or not isinstance(observation, dict)
            or not isinstance(observation.get("page"), dict)
        ):
            self.clear()
            return data
        current = {key: value for key, value in observation.items() if key in FIELDS}
        identity = (run_id, observation["page"].get("url"))
        if json_bytes(current) > self.max_bytes:
            self.clear()
            return data
        self.sequence += 1
        snapshot_id = f"s{self.sequence}"
        full = {**observation, "snapshot": {"id": snapshot_id, "mode": "full"}}
        encoded = full
        if (
            self.base_id
            and identity == self.identity
            and operation not in RESET_OPERATIONS
            and self.deltas < self.max_deltas
        ):
            changed = {
                key: value
                for key, value in observation.items()
                if key not in FIELDS or key not in self.base or value != self.base[key]
            }
            delta = {
                **changed,
                "snapshot": {
                    "id": snapshot_id,
                    "mode": "delta",
                    "base_id": self.base_id,
                    "removed": sorted(self.base.keys() - current.keys()),
                },
            }
            if json_bytes(delta) < json_bytes(full):
                encoded = delta
        self.deltas = self.deltas + 1 if encoded is not full else 0
        self.base = deepcopy(current)
        self.base_id = snapshot_id
        self.identity = identity
        return {**data, "observation": encoded} if observation is not data else encoded
