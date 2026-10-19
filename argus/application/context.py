from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .manager import SessionManager

_runtime: ContextVar[SessionManager] = ContextVar("argus_runtime")


def current_runtime() -> SessionManager:
    return _runtime.get()


@contextmanager
def runtime_scope(runtime: SessionManager) -> Iterator[None]:
    token = _runtime.set(runtime)
    try:
        yield
    finally:
        _runtime.reset(token)
