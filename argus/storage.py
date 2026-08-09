from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    while not acquired:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, f"{os.getpid()}\n".encode())
            finally:
                os.close(descriptor)
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > stale_after_seconds
            except FileNotFoundError:
                continue
            if stale:
                try:
                    path.unlink()
                    continue
                except FileNotFoundError:
                    continue
            if time.monotonic() >= deadline:
                raise StorageError(f"timed out waiting for storage lock {path}")
            time.sleep(0.05)
        except OSError as exc:
            raise StorageError(
                f"could not create storage lock {path}: {type(exc).__name__}: {exc}"
            ) from exc
    try:
        yield
    finally:
        if acquired:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


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
