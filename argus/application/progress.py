from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable


class Stage(str, Enum):
    STARTED = "Operation started"
    LAUNCHING = "Launching the browser"
    NAVIGATING = "Loading the target page"
    OBSERVING = "Inspecting the current page"
    VERIFYING = "Rechecking the observable symptom"
    VERIFIED = "Independent recheck reproduced the symptom"
    NOT_REPRODUCED = "Independent recheck did not reproduce the symptom"
    INCONCLUSIVE = "Evidence is inconclusive; no confirmation"
    COVERAGE = "Coverage evidence updated"
    EXPORTING = "Writing reports and evidence references"
    COMPLETED = "Operation completed"
    FAILED = "Operation failed; inspect the error result"


ProgressSink = Callable[[int, str], Awaitable[None]]


@dataclass
class Progress:
    sink: ProgressSink | None = None
    stages: list[str] = field(default_factory=list)
    omitted: int = 0
    delivery_failed: bool = False

    async def emit(self, stage: Stage) -> None:
        if len(self.stages) >= 64:
            self.omitted += 1
            return
        self.stages.append(stage.name.lower())
        if self.sink is not None and not self.delivery_failed:
            try:
                async with asyncio.timeout(0.25):
                    await self.sink(len(self.stages), stage.value)
            except Exception:
                self.delivery_failed = True


_progress: ContextVar[Progress | None] = ContextVar("argus_progress", default=None)


@contextmanager
def progress_scope(sink: ProgressSink | None = None):
    progress = Progress(sink)
    token = _progress.set(progress)
    try:
        yield progress
    finally:
        _progress.reset(token)


async def emit(stage: Stage) -> None:
    progress = _progress.get()
    if progress is not None:
        await progress.emit(stage)
