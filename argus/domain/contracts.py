from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    FAILED = "failed"


class ErrorCode(str, Enum):
    NO_SESSION = "no_session"
    BUSY = "busy"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BACKEND = "backend_error"
    INVALID_ARGUMENT = "invalid_argument"
    STORAGE = "storage_error"
    PERMISSION = "permission_denied"


class OperationError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


class Rejection(str):
    code: ErrorCode

    def __new__(cls, message: str, code: ErrorCode = ErrorCode.INVALID_ARGUMENT):
        value = super().__new__(cls, message)
        value.code = code
        return value


@dataclass(frozen=True)
class ImageArtifact:
    path: str | Path


@dataclass(frozen=True)
class OperationResult:
    value: Any
    error: ErrorCode | None = None
    state: str = "idle"
    run_id: str | None = None


class ToolText(str):
    data: dict[str, Any]

    def __new__(cls, text: str, data: dict[str, Any]):
        value = super().__new__(cls, text)
        value.data = data
        return value


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    timestamp: float
    kind: str
    operation: str | None
    state: str
    code: str | None = None
