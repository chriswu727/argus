from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from argus.application.context import current_runtime, runtime_scope
from argus.application.manager import SessionManager
from argus.domain.contracts import ErrorCode, OperationError

from .evidence import _journal_entries
from .lifecycle import start_session
from .session import _require_session


@dataclass(frozen=True)
class RegressionRun:
    results: list[dict]
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.error:
            return 2
        if any(item["status"] == "STILL-PRESENT" for item in self.results):
            return 1
        if any(item["status"] == "INCONCLUSIVE" for item in self.results):
            return 3
        return 0


async def check_entries(session, entries: list[dict], url: str) -> list[dict]:
    results = []
    for entry in entries:
        clause = entry.get("verify") or {}
        if clause.get("replay") or clause.get("kind", "text") not in {"text", "http_status"}:
            receipt = {
                "attempted": False,
                "reason": "automatic regression only runs read-only built-in checks",
            }
        else:
            receipt = await current_runtime().verifiers.verify(session, clause)
        reproduced = receipt.get("reproduced")
        status = (
            "STILL-PRESENT"
            if reproduced is True
            else "FIXED"
            if reproduced is False
            else "INCONCLUSIVE"
        )
        results.append(
            {
                "title": entry.get("title", ""),
                "severity": entry.get("severity", "?"),
                "status": status,
                "runs": receipt.get("runs"),
                "url": (entry.get("verify") or {}).get("at_url") or url,
            }
        )
    return results


async def run_regression(runtime: SessionManager, url: str, headless: bool = True) -> RegressionRun:
    async def run():
        entries = _journal_entries(urlparse(url).netloc or "default", strict=True)
        if not entries:
            return RegressionRun([])
        await start_session(url, headless=headless, include_observation=False)
        if not runtime.session.active:
            raise OperationError(ErrorCode.BACKEND, "Could not start regression session")
        return RegressionRun(await check_entries(runtime.session, entries, url))

    try:
        with runtime_scope(runtime):
            result = await runtime.execute("regression", run)
        return RegressionRun([], str(result.value)) if result.error else result.value
    finally:
        await runtime.shutdown()


async def regression_check() -> str:
    """Re-test previously-recorded findings for this site against the CURRENT
    build — "did my fix land, and did anything I'd fixed come back?".

    Findings with a clean-load verify clause are journaled at end_session (per
    origin). This re-runs each one's INDEPENDENT clean-load check now and
    classifies it: STILL-PRESENT (the bug is still there), NO-LONGER-REPRODUCES
    (the symptom is gone — likely fixed; confirm the surface still exists), or
    INCONCLUSIVE. Each carried finding is treated as a hypothesis and re-checked
    from scratch — nothing is trusted from the prior run. Read-only (clean GETs);
    replay-mode findings are not auto-re-driven (that would re-execute writes).
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return "regression_check: this tool is web-mode only."
    origin = urlparse(s.browser.current_url).netloc or urlparse(s.url or "").netloc or "default"
    entries = _journal_entries(origin, strict=True)
    if not entries:
        return (
            f"regression_check: no journaled findings for {origin}. Findings with a "
            "verify clause are journaled when you call end_session."
        )

    lines = [f"Regression re-check for {origin} — {len(entries)} journaled finding(s):", ""]
    still = gone = incon = 0
    for e in await check_entries(s, entries, s.url or ""):
        if e["status"] == "STILL-PRESENT":
            tag = "STILL-PRESENT       "
            still += 1
        elif e["status"] == "FIXED":
            tag = "NO-LONGER-REPRODUCES"
            gone += 1
        else:
            tag = "INCONCLUSIVE        "
            incon += 1
        lines.append(f"  [{tag}] [{e.get('severity', '?').upper()}] {e.get('title', '')[:80]}")
    lines.append("")
    lines.append(
        f"{still} still present · {gone} no longer reproduce (likely fixed — "
        f"confirm the surface still exists) · {incon} inconclusive."
    )
    return "\n".join(lines)
