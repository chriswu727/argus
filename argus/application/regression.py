from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from argus.access import canonical_origin
from argus.application.context import current_runtime, runtime_scope
from argus.application.manager import SessionManager
from argus.domain.contracts import ErrorCode, OperationError, Rejection, ToolText
from argus.domain.verification import receipt_outcome
from argus.security import _redact

from .coverage import _coverage_reference_url
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
        if (
            clause.get("replay")
            or clause.get("clear_storage")
            or clause.get("kind", "text") not in {"text", "http_status"}
        ):
            receipt = {
                "attempted": False,
                "reason": "automatic regression only runs read-only built-in checks",
            }
        elif clause.get("version") != 1:
            receipt = {
                "attempted": False,
                "reason": "legacy verification lacks complete preconditions; preserve this journal and re-record findings in a new ARGUS_OUTPUT_DIR (see docs/usage.md)",
            }
        else:
            receipt = await current_runtime().verifiers.verify(
                session, clause, login_page=clause.get("login_page", False)
            )
        reproduced = receipt_outcome(receipt)
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
                "reason": receipt.get("reason")
                or ("symptom reproduced intermittently" if receipt.get("flaky") else None),
                "url": (entry.get("verify") or {}).get("at_url") or url,
            }
        )
    return results


def _regression_target_error(url: str) -> str | None:
    """Reject a URL that would silently select the wrong journal.

    "localhost:3000" parses with an empty host, reads the catch-all journal and
    reports no findings, which a CI gate would take as a pass.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme == "file":
        return None
    try:
        canonical_origin(url)
    except ValueError:
        return (
            f"regression: {url!r} is not a testable URL; pass the full address the findings "
            "were recorded at, such as http://localhost:3000"
        )
    return None


async def run_regression(runtime: SessionManager, url: str, headless: bool = True) -> RegressionRun:
    async def start():
        entries = _journal_entries(urlparse(url).netloc or "default", strict=True)
        if not entries:
            return []
        await start_session(url, headless=headless, include_observation=False)
        if not runtime.session.active:
            raise OperationError(ErrorCode.BACKEND, "Could not start regression session")
        return entries

    try:
        invalid = _regression_target_error(url)
        if invalid:
            return RegressionRun([], invalid)
        with runtime_scope(runtime):
            started = await runtime.execute("regression", start)
            if started.error:
                return RegressionRun([], str(started.value))
            results: list[dict] = []
            # One operation per finding: a journal only grows, and a single
            # deadline for the whole run would fail it once it is long enough.
            for entry in started.value:

                async def check(entry=entry):
                    return await check_entries(runtime.session, [entry], url)

                checked = await runtime.execute("regression", check)
                if checked.error:
                    results.append(
                        {
                            "title": entry.get("title", ""),
                            "severity": entry.get("severity", "?"),
                            "status": "INCONCLUSIVE",
                            "runs": None,
                            "reason": f"check did not complete: {checked.value}"[:300],
                            "url": (entry.get("verify") or {}).get("at_url") or url,
                        }
                    )
                else:
                    results.extend(checked.value)
        return RegressionRun(results)
    finally:
        await runtime.shutdown()


async def regression_check() -> str:
    """Re-test previously-recorded findings for this site against the CURRENT
    build — "did my fix land, and did anything I'd fixed come back?".

    Findings confirmed by a clean-load verify clause are journaled as they are
    recorded (per origin). This re-runs each one's INDEPENDENT clean-load check now and
    classifies it: STILL-PRESENT (the bug is still there), NO-LONGER-REPRODUCES
    (the symptom is gone — likely fixed; confirm the surface still exists), or
    INCONCLUSIVE. Each carried finding is treated as a hypothesis and re-checked
    from scratch — nothing is trusted from the prior run. Read-only (clean GETs);
    replay-mode findings are not auto-re-driven (that would re-execute writes).
    """
    s = _require_session()
    if s.mode != "web" or s.browser is None or not s.browser.has_page:
        return Rejection("regression_check: this tool is web-mode only.")
    origin = urlparse(s.browser.current_url).netloc or urlparse(s.url or "").netloc or "default"
    # The journal is keyed by host and port; credentials never belong in a result.
    site = origin.rsplit("@", 1)[-1]
    entries = _journal_entries(origin, strict=True)
    if not entries:
        s.regression_checks.append({"results": [], "reason": "no_journal"})
        s.steps.append("regression_check: no journaled findings to recheck")
        return ToolText(
            f"regression_check: no journaled findings for {site}. Findings with a "
            "verify clause are journaled once record_bug confirms them.",
            {
                "site": site,
                "checked": 0,
                "counts": {"still_present": 0, "no_longer_reproduces": 0, "inconclusive": 0},
                "results": [],
                "reason": "no_journal",
                "detail": "No confirmed findings are journaled for this site; a finding is "
                "journaled when record_bug confirms it. Nothing was rechecked.",
            },
        )

    lines = [f"Regression re-check for {site} — {len(entries)} journaled finding(s):", ""]
    counts = {"still_present": 0, "no_longer_reproduces": 0, "inconclusive": 0}
    results = []
    keys = []
    checked = await check_entries(s, entries, s.url or "")
    for entry, e in zip(entries, checked, strict=True):
        keys.append(entry["fingerprint"])
        if e["status"] == "STILL-PRESENT":
            status, tag, key = "STILL-PRESENT", "STILL-PRESENT       ", "still_present"
        elif e["status"] == "FIXED":
            status, tag, key = (
                "NO-LONGER-REPRODUCES",
                "NO-LONGER-REPRODUCES",
                "no_longer_reproduces",
            )
        else:
            status, tag, key = "INCONCLUSIVE", "INCONCLUSIVE        ", "inconclusive"
        counts[key] += 1
        lines.append(
            f"  [{tag}] [{e.get('severity', '?').upper()}] {_redact(e.get('title', ''))[:80]}"
        )
        if e.get("reason"):
            lines.append(f"    {e['reason']}")
        results.append(
            {
                "title": str(e.get("title", ""))[:120],
                "severity": e.get("severity", "?"),
                "status": status,
                "runs": e.get("runs"),
                "reason": e.get("reason"),
                # Path only: a query string can carry tokens.
                "path": _coverage_reference_url(e.get("url") or ""),
            }
        )
    lines.append("")
    lines.append(
        f"{counts['still_present']} still present · {counts['no_longer_reproduces']} no longer "
        f"reproduce (likely fixed — confirm the surface still exists) · "
        f"{counts['inconclusive']} inconclusive."
    )
    s.regression_checks.append(
        {"results": [{**result, "key": key} for key, result in zip(keys, results, strict=True)]}
    )
    s.steps.append(f"regression_check: {len(results)} journaled finding(s) rechecked")
    return ToolText(
        "\n".join(lines),
        {"site": site, "checked": len(results), "counts": counts, "results": results},
    )
