from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from argus.access import AccessPolicy


@dataclass(frozen=True)
class CaptureSettings:
    network_events: int = 2000
    error_events: int = 500
    download_events: int = 50
    dialog_events: int = 100
    body_bytes: int = 16 * 1024
    read_bytes: int = 256 * 1024
    unknown_length_body: bool = False

    def __post_init__(self) -> None:
        for name in ("network_events", "error_events", "download_events", "dialog_events"):
            if not 1 <= getattr(self, name) <= 1_000_000:
                raise ValueError(f"{name} must be between 1 and 1000000")
        if not 1 <= self.body_bytes <= self.read_bytes <= 16 * 1024 * 1024:
            raise ValueError("body_bytes <= read_bytes <= 16 MiB must hold, with positive limits")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CaptureSettings:
        env = os.environ if env is None else env
        return cls(
            network_events=int(env.get("ARGUS_MAX_NETWORK_EVENTS", "2000")),
            error_events=int(env.get("ARGUS_MAX_ERROR_EVENTS", "500")),
            download_events=int(env.get("ARGUS_MAX_DOWNLOAD_EVENTS", "50")),
            dialog_events=int(env.get("ARGUS_MAX_DIALOG_EVENTS", "100")),
            body_bytes=int(env.get("ARGUS_MAX_RESPONSE_BODY_BYTES", "16384")),
            read_bytes=int(env.get("ARGUS_MAX_RESPONSE_READ_BYTES", "262144")),
            unknown_length_body=env.get("ARGUS_CAPTURE_UNKNOWN_LENGTH_BODY", "").lower()
            in {"1", "true", "yes", "on"},
        )


@dataclass(frozen=True)
class Settings:
    output_dir: Path = Path("argus-reports")
    operation_timeout: float = 120.0
    queue_timeout: float = 5.0
    cleanup_timeout: float = 10.0
    event_capacity: int = 512
    monitor_port: int | None = None
    unsafe_eval: bool = False
    response_mode: str = "compatible"
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    access: AccessPolicy = field(default_factory=AccessPolicy)

    def __post_init__(self) -> None:
        import math

        if self.response_mode not in {"compatible", "compact", "delta"}:
            raise ValueError("response_mode must be compatible, compact, or delta")

        for name in ("operation_timeout", "queue_timeout", "cleanup_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 1 <= self.event_capacity <= 100_000:
            raise ValueError("event_capacity must be between 1 and 100000")
        if self.monitor_port is not None and not 0 <= self.monitor_port <= 65535:
            raise ValueError("monitor_port must be between 0 and 65535")
        object.__setattr__(self, "output_dir", self.output_dir.expanduser().resolve())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides) -> Settings:
        env = os.environ if env is None else env
        values: dict[str, Any] = {
            "output_dir": Path(env.get("ARGUS_OUTPUT_DIR", "argus-reports")),
            "operation_timeout": float(env.get("ARGUS_OPERATION_TIMEOUT_S", "120")),
            "queue_timeout": float(env.get("ARGUS_QUEUE_TIMEOUT_S", "5")),
            "cleanup_timeout": float(env.get("ARGUS_CLEANUP_TIMEOUT_S", "10")),
            "event_capacity": int(env.get("ARGUS_EVENT_CAPACITY", "512")),
            "monitor_port": int(env["ARGUS_MONITOR_PORT"])
            if env.get("ARGUS_MONITOR_PORT")
            else None,
            "unsafe_eval": env.get("ARGUS_UNSAFE_EVAL", "").lower() in {"1", "true"},
            "response_mode": env.get("ARGUS_RESPONSE_MODE", "compatible"),
            "capture": CaptureSettings.from_env(env),
            "access": AccessPolicy.from_env(env),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
