from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from argus.domain.contracts import ErrorCode, OperationError


def canonical_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(c.isspace() or c == "\\" for c in url)
        ):
            raise ValueError
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError as exc:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials") from exc


@dataclass(frozen=True)
class AccessPolicy:
    allowed_origins: tuple[str, ...] | None = None
    file_roots: tuple[Path, ...] = ()
    sensitive_reads: bool = False

    def __post_init__(self):
        if self.allowed_origins is not None:
            for value in self.allowed_origins:
                canonical_origin(value)
                parsed = urlsplit(value)
                if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                    raise ValueError("Allowed origins cannot include paths, queries or fragments")
        object.__setattr__(
            self, "file_roots", tuple(Path(p).expanduser().resolve() for p in self.file_roots)
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AccessPolicy:
        env = os.environ if env is None else env

        def strings(name, default):
            try:
                value = json.loads(env[name]) if name in env else default
            except ValueError as exc:
                raise ValueError(f"{name} must be a JSON array of strings") from exc
            if name in env and value is None:
                raise ValueError(f"{name} cannot be null")
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(s, str) and s for s in value)
            ):
                raise ValueError(f"{name} must be a JSON array of nonempty strings")
            return value

        origins = strings("ARGUS_ALLOWED_ORIGINS", None)
        roots = strings("ARGUS_FILE_ROOTS", [])
        if roots is None:
            raise ValueError("ARGUS_FILE_ROOTS cannot be null")
        return cls(
            tuple(origins) if origins is not None else None,
            tuple(Path(p) for p in roots),
            env.get("ARGUS_ALLOW_SENSITIVE_READS", "").lower() in {"1", "true"},
        )

    def check_file(self, path: str | Path, *, extra_roots: tuple[Path, ...] = ()) -> Path:
        resolved = Path(path).expanduser().resolve(strict=True)
        roots = self.file_roots + tuple(p.resolve() for p in extra_roots)
        if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in roots):
            raise OperationError(
                ErrorCode.PERMISSION, "File access denied; configure ARGUS_FILE_ROOTS at startup."
            )
        return resolved

    def check_url(self, url: str) -> None:
        if url == "about:blank":
            return
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise OperationError(ErrorCode.PERMISSION, "Invalid target URL.") from exc
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                raise OperationError(ErrorCode.PERMISSION, "Remote file URLs are not allowed.")
            self.check_file(unquote(parsed.path))
            return
        try:
            origin = canonical_origin(url)
        except ValueError as exc:
            raise OperationError(ErrorCode.PERMISSION, str(exc)) from exc
        if self.allowed_origins is not None and origin not in {
            canonical_origin(value) for value in self.allowed_origins
        }:
            raise OperationError(
                ErrorCode.PERMISSION, "Target origin is outside ARGUS_ALLOWED_ORIGINS."
            )
