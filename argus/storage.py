from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

JOURNAL_SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    pass


def storage_warning(message: str) -> None:
    print(f"argus: storage warning: {message}", file=sys.stderr)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"could not read {path}: {type(exc).__name__}: {exc}") from exc


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError) as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageError(f"could not write {path}: {type(exc).__name__}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))


@contextmanager
def exclusive_file_lock(
    path: Path,
    timeout_seconds: float = 5.0,
    stale_after_seconds: float = 60.0,
):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The OS releases this lock on process exit. Never unlink its inode:
        # waiters may already hold it open, which would create two lock domains.
        with FileLock(path, timeout=timeout_seconds):
            yield
    except Timeout as exc:
        raise StorageError(f"timed out waiting for storage lock {path}") from exc
    except OSError as exc:
        raise StorageError(f"storage lock failed for {path}: {exc}") from exc


def read_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    document = read_json(path)
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        version = document.get("schema_version")
        if version != JOURNAL_SCHEMA_VERSION:
            raise StorageError(
                f"unsupported journal schema {version!r} in {path}; "
                f"expected {JOURNAL_SCHEMA_VERSION}"
            )
        entries = document.get("entries")
    else:
        entries = None
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise StorageError(f"invalid journal structure in {path}")
    return entries


def write_journal(path: Path, entries: list[dict]) -> None:
    atomic_write_json(
        path,
        {"schema_version": JOURNAL_SCHEMA_VERSION, "entries": entries},
    )
