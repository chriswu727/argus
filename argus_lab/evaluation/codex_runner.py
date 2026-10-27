from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from argus.storage import atomic_write_text

from .agent_fixture import CASES, REVISION, TASKS, agent_fixture


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    expected: str
    observed: str
    evidence_calls: list[int] = Field(min_length=1, max_length=20)


class GoalClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    evidence_calls: list[int] = Field(min_length=1, max_length=20)


class AgentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[Claim] = Field(max_length=10)
    covered_goals: list[GoalClaim] = Field(max_length=20)
    limitations: list[str] = Field(max_length=20)


PROMPT = """Test the application at {origin} using only the provided testing MCP tools.
Task: {task}
Goals: {goals}
Use synthetic values only. Stay on this origin. Do not inspect source code, files,
other services or tools. The app may be correct or defective. Exercise the task,
check the observable behavior against the stated requirement, and report only
evidenced defects. Do not fix the app. Do not infer a bug merely from a tool error.
You have at most 60 tool calls and five minutes. Finish sooner when the task is covered.
Evidence call numbers are 1-based in execution order; cite the calls that support
each finding and exercised goal. Return the required JSON report even when no
defects are found. If using Argus, end the session before your final answer.
"""
TASK_REVISION = hashlib.sha256((PROMPT + json.dumps(TASKS, sort_keys=True)).encode()).hexdigest()


def clean_environment() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("HOME", "PATH", "CODEX_HOME", "PLAYWRIGHT_BROWSERS_PATH", "TMPDIR")
        if key in os.environ
    }


def command(
    model: str, effort: str, origin: str, arm: str, output: Path, workspace: Path
) -> list[str]:
    overrides = {
        "model_provider": "openai",
        "model_reasoning_effort": effort,
        "forced_login_method": "chatgpt",
        "approval_policy": "never",
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "hide_agent_reasoning": True,
        "history.persistence": "none",
        "features.shell_tool": False,
        "features.apps": False,
        "features.plugins": False,
        "features.hooks": False,
        "features.multi_agent": False,
        "features.memories": False,
        "features.browser_use": False,
        "features.computer_use": False,
        "features.in_app_browser": False,
        "features.image_generation": False,
        "features.view_image": False,
        "features.skill_search": False,
        "features.skip_host_skill_discovery": True,
        "mcp_servers.testing.command": sys.executable,
        "mcp_servers.testing.args": [
            "-m",
            "argus_lab.evaluation.gateway",
            "--arm",
            arm,
            "--origin",
            origin,
            "--output",
            str(output),
        ],
        "mcp_servers.testing.tool_timeout_sec": 120,
        "mcp_servers.testing.required": True,
        "mcp_servers.testing.default_tools_approval_mode": "approve",
    }
    args = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--model",
        model,
        "--cd",
        str(workspace),
        "--output-schema",
        str(output.parent / "report-schema.json"),
        "--output-last-message",
        str(output / "answer.json"),
    ]
    for key, value in overrides.items():
        args.extend(["-c", f"{key}={json.dumps(value)}"])
    return args


async def stop_process(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        await process.wait()


async def bounded_stderr(stream, path):
    remaining = 64_000
    with path.open("wb") as output:
        while chunk := await stream.read(4096):
            output.write(chunk[:remaining])
            remaining = max(0, remaining - len(chunk))


async def run_trial(
    model: str, effort: str, case, arm: str, repetition: int, output: Path, host: str
):
    output.mkdir(mode=0o700)
    started = time.monotonic()
    record = {
        "case_id": case.id,
        "arm": arm,
        "repetition": repetition,
        "model": model,
        "model_settings": {"reasoning_effort": effort, "model_seed_supported": False},
        "host": host,
        "fixture_revision": REVISION,
        "task_revision": TASK_REVISION,
        "seed": repetition,
        "budget": {"tool_calls": 60, "wall_seconds": 300},
        "completed": False,
        "error": "interrupted",
        "input_tokens": None,
        "output_tokens": None,
    }
    atomic_write_text(output / "attempt.json", json.dumps(record))
    with (
        agent_fixture(case.id) as (origin, oracle_events),
        tempfile.TemporaryDirectory(prefix="argus-agent-empty-") as workspace,
    ):
        task = TASKS[case.id.rsplit("-", 1)[0]][0]
        prompt = PROMPT.format(origin=origin, task=task, goals=", ".join(case.goals))
        process = await asyncio.create_subprocess_exec(
            *command(model, effort, origin, arm, output, Path(workspace)),
            prompt,
            env=clean_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        stderr = asyncio.create_task(bounded_stderr(process.stderr, output / "stderr.log"))
        seen_calls = set()
        bytes_seen = 0
        try:
            async with asyncio.timeout(300):
                with (output / "events.jsonl").open("w") as trace:
                    while line := await process.stdout.readline():
                        bytes_seen += len(line)
                        if bytes_seen > 16_000_000:
                            raise RuntimeError("event_output_limit")
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        item = event.get("item", {})
                        if item.get("type") == "reasoning":
                            continue
                        trace.write(json.dumps(event) + "\n")
                        trace.flush()
                        if item.get("type") in {"command_execution", "web_search", "file_change"}:
                            raise RuntimeError("disallowed_host_tool")
                        if item.get("type") == "mcp_tool_call":
                            seen_calls.add(item["id"])
                            if item.get("status") == "failed":
                                raise RuntimeError("host_tool_failure")
                            if len(seen_calls) > 60:
                                raise RuntimeError("tool_budget_exhausted")
                        if event.get("type") == "turn.completed":
                            usage = event.get("usage", {})
                            record["input_tokens"] = usage.get("input_tokens")
                            record["output_tokens"] = usage.get("output_tokens")
                await process.wait()
                if process.returncode:
                    raise RuntimeError("host_failed")
                if not (output / "tools.jsonl").exists():
                    raise RuntimeError("no_tool_execution")
                answer = AgentReport.model_validate_json((output / "answer.json").read_text())
                record.update(completed=True, error=None, report=answer.model_dump())
        except TimeoutError:
            record["error"] = "timeout"
        except (ValueError, OSError, RuntimeError) as exc:
            record["error"] = (
                str(exc)[:200] if isinstance(exc, RuntimeError) else type(exc).__name__
            )
        finally:
            await stop_process(process)
            await stderr
            record["duration_seconds"] = time.monotonic() - started
            tool_path = output / "tools.jsonl"
            record["tool_calls"] = (
                sum(1 for _ in tool_path.open()) if tool_path.exists() else len(seen_calls)
            )
            atomic_write_text(output / "oracle.json", json.dumps(oracle_events, indent=2))
            atomic_write_text(output / "result.json", json.dumps(record, indent=2))
    return record


async def collect(output: Path, model: str, effort: str, limit: int, max_runs: int = 32):
    if not 1 <= limit <= max_runs <= 32:
        raise ValueError("run count must fit the authorized cap, at most 32")
    env = clean_environment()
    auth = subprocess.run(
        ["codex", "login", "status"], env=env, capture_output=True, text=True, timeout=20
    )
    if auth.returncode or "ChatGPT" not in auth.stdout + auth.stderr:
        raise RuntimeError("ChatGPT login required; API fallback is not allowed")
    host = subprocess.check_output(["codex", "--version"], env=env, text=True, timeout=20).strip()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / "collection.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = {
            "model": model,
            "effort": effort,
            "host": host,
            "fixture": REVISION,
            "task": TASK_REVISION,
            "max_runs": max_runs,
            "implementation": hashlib.sha256(
                b"".join(
                    path.read_bytes() for path in sorted(Path(__file__).parents[1].rglob("*.py"))
                )
            ).hexdigest(),
        }
        path = output / "manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ValueError("collection controls changed; do not mix experiments")
        atomic_write_text(path, json.dumps(manifest, indent=2))
        atomic_write_text(
            output / "report-schema.json", json.dumps(AgentReport.model_json_schema())
        )
        families = {
            family: index for index, family in enumerate(("profile", "form", "tasks", "cart"))
        }
        cases = sorted(CASES, key=lambda case: (families[case.id.rsplit("-", 1)[0]], case.id))
        schedule = [
            (case, arm, repetition)
            for repetition in range(2)
            for index, case in enumerate(cases)
            for arm in (
                ("baseline", "argus") if (index + repetition) % 2 == 0 else ("argus", "baseline")
            )
        ]
        for index, (case, arm, repetition) in enumerate(schedule[:limit]):
            run_dir = output / f"run-{index:03d}"
            if run_dir.exists():
                if not (run_dir / "result.json").exists():
                    raise RuntimeError("an interrupted attempt requires review before continuing")
                continue
            print(
                json.dumps({"run": index + 1, "case": case.id, "arm": arm, "state": "started"}),
                flush=True,
            )
            result = await run_trial(model, effort, case, arm, repetition, run_dir, host)
            print(
                json.dumps(
                    {
                        "run": index + 1,
                        "state": "finished",
                        "completed": result["completed"],
                        "error": result["error"],
                        "calls": result["tool_calls"],
                        "seconds": round(result["duration_seconds"], 1),
                    }
                ),
                flush=True,
            )
            if result["error"] in {
                "host_failed",
                "host_tool_failure",
                "no_tool_execution",
                "disallowed_host_tool",
                "event_output_limit",
            }:
                raise RuntimeError("host/harness failure; inspect before consuming more runs")


def main():
    parser = argparse.ArgumentParser(
        description="Opt-in Codex subscription evaluation; never run in CI"
    )
    parser.add_argument("--allow-subscription-usage", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="high")
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--max-runs", type=int, default=32)
    args = parser.parse_args()
    asyncio.run(
        collect(
            args.output.expanduser().resolve(), args.model, args.effort, args.runs, args.max_runs
        )
    )


if __name__ == "__main__":
    main()
