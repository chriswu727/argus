from __future__ import annotations

from functools import partial

from argus.adapters.journal import FileJournal
from argus.adapters.reporting.reporter import Reporter
from argus.application.manager import SessionManager
from argus.application.verification import _run_replay_receipt, _run_reproduction_check
from argus.application.verifiers import VerificationRegistry
from argus.browser import BrowserDriver
from argus.settings import Settings


def create_runtime(settings: Settings | None = None) -> SessionManager:
    settings = settings or Settings.from_env()
    runtime = SessionManager(
        settings,
        partial(BrowserDriver, capture_settings=settings.capture, access_policy=settings.access),
        Reporter,
        FileJournal(),
    )
    runtime.verifiers = VerificationRegistry(
        {
            "text": _run_reproduction_check,
            "http_status": _run_reproduction_check,
            "replay": _run_replay_receipt,
        }
    )
    return runtime
