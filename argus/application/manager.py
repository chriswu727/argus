from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from typing import Any, Awaitable, Callable

from argus.domain.contracts import (
    ErrorCode,
    OperationError,
    OperationResult,
    Rejection,
    SessionState,
)
from argus.ports import JournalPort
from argus.security import _redact
from argus.settings import Settings
from argus.storage import StorageError

from .context import runtime_scope
from .guidance import monitor_counts
from .progress import Stage, emit
from .session import Session
from .telemetry import EventStream
from .verifiers import VerificationRegistry

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        browser_factory: Callable[..., Any],
        reporter_factory: Callable[..., Any],
        journal: JournalPort,
    ):
        self.settings = settings
        self.browser_factory = browser_factory
        self.reporter_factory = reporter_factory
        self.journal = journal
        self.verifiers = VerificationRegistry({})
        self.session = Session()
        self.state = SessionState.IDLE
        self.lock = asyncio.Lock()
        self.events = EventStream(settings.event_capacity)
        self.calls: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()
        self.durations: dict[str, float] = defaultdict(float)
        self.response_totals: Counter[str] = Counter()
        self.component_seconds: Counter[str] = Counter()
        self.component_calls: Counter[str] = Counter()
        self.current_operation: str | None = None
        self.operation_started: float | None = None
        self.started = time.monotonic()
        self.last_progress = time.time()
        self.last_error: str | None = None
        self.last_stage: str | None = None
        self.ready = True
        self.closing = False
        self._active_task: asyncio.Task | None = None

    async def progress(self, stage: Stage) -> None:
        self.last_progress = time.time()
        self.last_stage = stage.name.lower()
        self.events.publish(
            "progress", self.state.value, self.current_operation, stage.name.lower()
        )
        await emit(stage, monitor_counts(self.session))

    def _result(self, value: Any, error: ErrorCode | None = None) -> OperationResult:
        return OperationResult(
            value, error, self.state.value, self.session.run_id if self.session.active else None
        )

    def transition(self, state: SessionState) -> None:
        if state == self.state:
            return
        allowed = {
            SessionState.IDLE: {
                SessionState.STARTING,
                SessionState.STOPPING,
                SessionState.FAILED,
                SessionState.ACTIVE,
            },
            SessionState.STARTING: {
                SessionState.ACTIVE,
                SessionState.STOPPING,
                SessionState.FAILED,
                SessionState.IDLE,
            },
            SessionState.ACTIVE: {
                SessionState.STARTING,
                SessionState.STOPPING,
                SessionState.FAILED,
                SessionState.IDLE,
            },
            SessionState.STOPPING: {SessionState.IDLE, SessionState.FAILED},
            SessionState.FAILED: {SessionState.STARTING, SessionState.STOPPING, SessionState.IDLE},
        }
        if state not in allowed[self.state]:
            raise RuntimeError(f"invalid session transition: {self.state.value} -> {state.value}")
        self.state = state
        self.last_progress = time.time()
        self.events.publish("session", state.value, self.current_operation)

    async def close_backends(self) -> list[str]:
        self.session.inputs.clear()
        errors = []
        for attribute in ("browser", "screen"):
            backend = getattr(self.session, attribute)
            if backend is None:
                continue
            try:
                await asyncio.wait_for(backend.stop(), self.settings.cleanup_timeout)
                setattr(self.session, attribute, None)
            except Exception as exc:
                errors.append(f"{attribute}: {type(exc).__name__}: {_redact(str(exc))[:120]}")
                logger.warning("backend cleanup failed", extra={"backend": attribute})
        if errors:
            self.last_error = ErrorCode.BACKEND.value
        return errors

    async def execute(
        self, name: str, operation: Callable[..., Awaitable[Any]], /, *args: Any, **kwargs: Any
    ) -> OperationResult:
        if self.closing:
            return self._result("Argus is shutting down.", ErrorCode.BUSY)
        try:
            await asyncio.wait_for(self.lock.acquire(), self.settings.queue_timeout)
        except asyncio.TimeoutError:
            return self._result(
                "Another session operation is in progress; retry later.", ErrorCode.BUSY
            )
        started = time.monotonic()
        self.current_operation = name
        self._active_task = asyncio.current_task()
        self.operation_started = started
        self.calls[name] += 1
        self.events.publish("operation_started", self.state.value, name)
        logger.info("operation_started", extra={"operation": name, "run_id": self.session.run_id})
        error = None
        try:
            await self.progress(Stage.STARTED)
            if self.closing:
                raise OperationError(ErrorCode.BUSY, "Argus is shutting down.")
            if self.state == SessionState.FAILED and name not in {
                "start_session",
                "start_screen_session",
                "end_session",
            }:
                raise OperationError(
                    ErrorCode.BACKEND, "Session failed; end it or start a new session."
                )
            with runtime_scope(self):
                if name not in {"start_session", "start_screen_session", "end_session"}:
                    url = getattr(self.session.browser, "current_url", "")
                    if url:
                        self.settings.access.check_url(url)
                value = await asyncio.wait_for(
                    operation(*args, **kwargs), self.settings.operation_timeout
                )
                if name != "end_session":
                    url = getattr(self.session.browser, "current_url", "")
                    if url:
                        self.settings.access.check_url(url)
            if isinstance(value, Rejection):
                error = value.code
                return self._result(str(value), error)
            if self.state != SessionState.FAILED:
                self.transition(SessionState.ACTIVE if self.session.active else SessionState.IDLE)
            return self._result(value)
        except asyncio.CancelledError:
            error = ErrorCode.CANCELLED
            await self.close_backends()
            self.transition(SessionState.FAILED)
            raise
        except asyncio.TimeoutError:
            error = ErrorCode.TIMEOUT
            await self.close_backends()
            self.transition(SessionState.FAILED)
            return self._result(
                f"{name}: operation timed out; session stopped. No automatic retry was attempted.",
                error,
            )
        except OperationError as exc:
            error = exc.code
            return self._result(f"{name}: {exc}", error)
        except StorageError:
            error = ErrorCode.STORAGE
            return self._result(
                f"{name}: storage could not be read or written; check file permissions and schema.",
                error,
            )
        except Exception as exc:
            error = ErrorCode.BACKEND
            if name in {"start_session", "start_screen_session", "end_session"}:
                await self.close_backends()
                self.transition(SessionState.FAILED)
            logger.warning(
                "operation failed", extra={"operation": name, "error_type": type(exc).__name__}
            )
            return self._result(
                f"{name}: {type(exc).__name__}; inspect the session and retry if appropriate.",
                error,
            )
        finally:
            try:
                if error != ErrorCode.CANCELLED:
                    await self.progress(Stage.FAILED if error else Stage.COMPLETED)
            except asyncio.CancelledError:
                error = ErrorCode.CANCELLED
                await self.close_backends()
                self.transition(SessionState.FAILED)
                raise
            finally:
                self.durations[name] += time.monotonic() - started
                if error:
                    self.failures[name] += 1
                    self.last_error = error.value
                self.last_progress = time.time()
                self.events.publish(
                    "operation_finished", self.state.value, name, error.value if error else None
                )
                logger.info(
                    "operation_finished",
                    extra={
                        "operation": name,
                        "run_id": self.session.run_id,
                        "code": error.value if error else "ok",
                    },
                )
                self.current_operation = None
                self._active_task = None
                self.operation_started = None
                self.lock.release()

    def snapshot(self) -> dict[str, Any]:
        session = self.session
        drops = (
            session.browser.capture_drops()
            if session.browser and hasattr(session.browser, "capture_drops")
            else {}
        )
        return {
            "schema_version": 1,
            "ready": self.ready and not self.closing,
            "state": self.state.value,
            "mode": session.mode,
            "uptime_seconds": time.monotonic() - self.started,
            "run_id": session.run_id if session.active else None,
            "operation": self.current_operation,
            "operation_seconds": time.monotonic() - self.operation_started
            if self.operation_started
            else 0,
            "last_progress_at": self.last_progress,
            "last_error": self.last_error,
            "stage": self.last_stage,
            "testing": monitor_counts(session),
            "calls": sum(self.calls.values()),
            "failures": sum(self.failures.values()),
            "findings": len(session.bugs),
            "observations": len(session.observations),
            "screenshots": len(session.screenshots),
            "capture_drops": drops,
            "events_dropped": self.events.dropped,
            "overhead": {
                "response_totals": dict(self.response_totals),
                "component_seconds": dict(self.component_seconds),
                "component_calls": dict(self.component_calls),
            },
        }

    async def shutdown(self) -> None:
        self.closing = True
        self.ready = False
        active = self._active_task
        if active is not None and active is not asyncio.current_task():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        async with self.lock:
            self.transition(SessionState.STOPPING)
            errors = await self.close_backends()
            self.transition(SessionState.FAILED if errors else SessionState.IDLE)
        self.events.close()
