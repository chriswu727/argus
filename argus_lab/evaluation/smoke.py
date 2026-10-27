from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright

from .fixture import REVISION, fixture_server
from .models import Budget, Case, Dataset, Finding, Trial


async def run_smoke() -> Dataset:
    cases = [
        Case(
            id=mode,
            expected_bugs=["lost-save"] if mode == "buggy" else [],
            goals=["save-and-reload"],
        )
        for mode in ("buggy", "fixed", "clean")
    ]
    trials = []
    with fixture_server() as base, tempfile.TemporaryDirectory(prefix="argus-eval-") as directory:
        for case in cases:
            for arm in ("baseline", "argus"):
                started = time.monotonic()
                calls = 0
                missing = None
                failure = None
                url = f"{base}/{case.id}/{arm}"
                try:
                    async with asyncio.timeout(30):
                        if arm == "baseline":
                            async with async_playwright() as playwright:
                                browser = await playwright.chromium.launch()
                                try:
                                    page = await browser.new_page()
                                    calls += 1
                                    await page.goto(url)
                                    calls += 1
                                    await page.get_by_role(
                                        "button", name="Save", exact=True
                                    ).click()
                                    calls += 1
                                    await page.get_by_text("Saved value", exact=True).wait_for()
                                    calls += 1
                                    await page.reload()
                                    calls += 1
                                    text = await page.locator("#status").inner_text()
                                    missing = text != "Saved value"
                                finally:
                                    await browser.close()
                        else:
                            env = {
                                key: os.environ[key]
                                for key in ("HOME", "PATH", "PLAYWRIGHT_BROWSERS_PATH")
                                if key in os.environ
                            }
                            env["ARGUS_OUTPUT_DIR"] = str(Path(directory) / case.id)
                            server = StdioServerParameters(
                                command=sys.executable, args=["-m", "argus.mcp_server"], env=env
                            )
                            with (Path(directory) / f"{case.id}.log").open("w") as log:
                                async with stdio_client(server, errlog=log) as (read, write):
                                    async with ClientSession(read, write) as client:
                                        await client.initialize()

                                        async def call(name, arguments):
                                            nonlocal calls
                                            calls += 1
                                            result = await client.call_tool(name, arguments)
                                            if result.isError:
                                                raise RuntimeError(f"{name} failed")
                                            return result

                                        await call(
                                            "start_session",
                                            {"url": url, "goals": ["save-and-reload"]},
                                        )
                                        await call(
                                            "coverage_update",
                                            {"goal": "save-and-reload", "status": "in_progress"},
                                        )
                                        observed = await call("click_what", {"description": "Save"})
                                        if (
                                            "Saved value"
                                            not in observed.structuredContent["data"][
                                                "visible_text"
                                            ]
                                        ):
                                            raise RuntimeError(
                                                "Save did not produce visible feedback"
                                            )
                                        checked = await call(
                                            "verify_persistence",
                                            {"expect": "present", "target_text": "Saved value"},
                                        )
                                        matched = checked.structuredContent["data"]["verification"][
                                            "matches"
                                        ]
                                        if matched is None:
                                            raise RuntimeError("Persistence check was inconclusive")
                                        missing = not matched
                                        ended = await call(
                                            "end_session",
                                            {
                                                "coverage_updates": [
                                                    {
                                                        "goal": "save-and-reload",
                                                        "status": "exercised",
                                                        "evidence": "Compared the saved state after a fresh page load.",
                                                    }
                                                ]
                                            },
                                        )
                                        if ended.structuredContent["data"]["warnings"]:
                                            raise RuntimeError("Report or teardown warnings")
                                        if (
                                            ended.structuredContent["data"]["coverage"]["goals"][0][
                                                "status"
                                            ]
                                            != "exercised"
                                        ):
                                            raise RuntimeError("Final coverage was not recorded")
                except Exception as exc:
                    failure = f"{type(exc).__name__}: smoke journey did not complete"
                evidence = (
                    f"Save feedback appeared; after reload, Saved value present={not missing}."
                )
                findings = (
                    [
                        Finding(
                            id="save-check",
                            matched_bug="lost-save" if case.id == "buggy" else None,
                            evidence=[evidence],
                        )
                    ]
                    if missing
                    else []
                )
                trials.append(
                    Trial(
                        arm=arm,
                        case_id=case.id,
                        repetition=0,
                        model="scripted-no-model",
                        host="argus-evaluation-smoke-v2",
                        fixture_revision=REVISION,
                        task_revision="save-reload-final-coverage-v2",
                        seed=0,
                        budget=Budget(tool_calls=10, wall_seconds=30),
                        completed=failure is None,
                        error=failure,
                        duration_seconds=time.monotonic() - started,
                        tool_calls=calls,
                        findings=findings,
                        covered_goals={"save-and-reload": [evidence]}
                        if missing is not None
                        else {},
                        adjudicator="fixture-ground-truth-v1",
                    )
                )
    return Dataset(
        experiment="persistence-protocol-smoke", mode="scripted_smoke", cases=cases, trials=trials
    )
