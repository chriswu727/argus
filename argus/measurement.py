from __future__ import annotations

import inspect
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps


@dataclass
class Measurements:
    seconds: Counter = field(default_factory=Counter)
    calls: Counter = field(default_factory=Counter)


_current: ContextVar[Measurements | None] = ContextVar("argus_measurements", default=None)


@contextmanager
def measurement_scope():
    measurements = Measurements()
    token = _current.set(measurements)
    try:
        yield measurements
    finally:
        _current.reset(token)


@contextmanager
def measure(category: str):
    measurements = _current.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if measurements is not None:
            measurements.seconds[category] += time.perf_counter() - started
            measurements.calls[category] += 1


def timed(category: str):
    def decorate(function):
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with measure(category):
                    return await function(*args, **kwargs)

            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            with measure(category):
                return function(*args, **kwargs)

        return synchronous

    return decorate
