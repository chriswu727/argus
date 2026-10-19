from __future__ import annotations

from pathlib import Path

from argus.storage import (
    StorageError,
    atomic_write_text,
    exclusive_file_lock,
    read_journal,
    write_journal,
)


class FileJournal:
    def read(self, path: Path) -> list[dict]:
        entries = read_journal(path)
        if any(
            not isinstance(entry.get("fingerprint"), str)
            or not isinstance(entry.get("verify"), dict)
            for entry in entries
        ):
            raise StorageError(f"invalid finding entry in {path}")
        return entries

    def merge(self, path: Path, entries: list[dict]) -> None:
        with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
            existing = self.read(path)
            merged = {entry["fingerprint"]: entry for entry in existing}
            merged.update({entry["fingerprint"]: entry for entry in entries})
            ignore = path.parent / ".gitignore"
            if not ignore.exists():
                atomic_write_text(ignore, "*\n")
            write_journal(path, list(merged.values()))
