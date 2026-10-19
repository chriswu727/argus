from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import asdict

from argus.domain.contracts import RuntimeEvent


class EventStream:
    def __init__(self, capacity: int = 512):
        self.events: deque[RuntimeEvent] = deque(maxlen=capacity)
        self.sequence = 0
        self.dropped = 0
        self.changed = asyncio.Event()
        self.closed = False

    def publish(
        self, kind: str, state: str, operation: str | None = None, code: str | None = None
    ) -> None:
        self.sequence += 1
        if len(self.events) == self.events.maxlen:
            self.dropped += 1
        self.events.append(RuntimeEvent(self.sequence, time.time(), kind, operation, state, code))
        self.changed.set()
        self.changed = asyncio.Event()

    def read(self, cursor: int) -> tuple[bool, list[dict]]:
        oldest = self.events[0].sequence if self.events else self.sequence + 1
        gap = cursor < oldest - 1 or cursor > self.sequence
        return gap, [asdict(event) for event in self.events if gap or event.sequence > cursor]

    def close(self) -> None:
        self.closed = True
        self.changed.set()
