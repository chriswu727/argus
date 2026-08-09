from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Generic, TypeVar, overload


T = TypeVar("T")


def positive_env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


class BoundedEventLog(Sequence[T], Generic[T]):
    def __init__(self, maxlen: int):
        self.maxlen = max(1, maxlen)
        self._items: deque[tuple[int, T]] = deque(maxlen=self.maxlen)
        self._next_cursor = 0
        self.dropped = 0

    def append(self, item: T) -> None:
        if len(self._items) == self.maxlen:
            self.dropped += 1
        self._items.append((self._next_cursor, item))
        self._next_cursor += 1

    def clear(self) -> None:
        self._items.clear()

    def copy(self) -> list[T]:
        return list(self)

    def cursor(self) -> int:
        return self._next_cursor

    def since(self, cursor: int) -> list[T]:
        return [item for index, item in self._items if index >= cursor]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return (item for _, item in self._items)

    def __reversed__(self) -> Iterator[T]:
        return (item for _, item in reversed(self._items))

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> list[T]: ...

    def __getitem__(self, index: int | slice) -> T | list[T]:
        values = list(self)
        return values[index]
