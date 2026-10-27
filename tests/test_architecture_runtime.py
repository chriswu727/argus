from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from argus.adapters.reporting.reporter import Reporter
from argus.application.context import current_runtime
from argus.application.evidence import _record_action
from argus.application.lifecycle import start_session
from argus.application.telemetry import EventStream
from argus.bootstrap import create_runtime
from argus.domain.contracts import ErrorCode, SessionState
from argus.domain.models import Bug, BugType, ExplorationResult, Severity
from argus.domain.replay import InputVault
from argus.settings import Settings


class Backend:
    def __init__(self, fail_start=False, **kwargs):
        self.fail_start = fail_start
        self.closed = False

    async def start(self):
        if self.fail_start:
            raise RuntimeError("synthetic launch failure")

    async def stop(self):
        self.closed = True


async def test_replacement_failure_never_keeps_closed_backend_active(isolated_runtime):
    runtime = isolated_runtime
    old = Backend()
    runtime.session.browser = old
    replacement = Backend(fail_start=True)
    runtime.browser_factory = lambda **kwargs: replacement

    result = await runtime.execute("start_session", start_session, "https://example.test")

    assert result.error == ErrorCode.BACKEND
    assert old.closed and replacement.closed
    assert not runtime.session.active
    assert runtime.state == SessionState.FAILED


async def test_action_and_shutdown_cannot_overlap(isolated_runtime):
    runtime = isolated_runtime
    backend = runtime.session.browser = Backend()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def action():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not backend.closed
            cancelled.set()

    task = asyncio.create_task(runtime.execute("click_what", action))
    await entered.wait()
    await runtime.shutdown()
    assert task.cancelled() and cancelled.is_set() and backend.closed
    assert not runtime.lock.locked()
    assert runtime.events.closed


async def test_timeout_cleans_up_and_releases_lock(isolated_runtime):
    runtime = isolated_runtime
    runtime.settings = replace(runtime.settings, operation_timeout=0.01)
    backend = runtime.session.browser = Backend()

    async def hangs():
        await asyncio.Event().wait()

    result = await runtime.execute("click_what", hangs)
    assert result.error == ErrorCode.TIMEOUT
    assert backend.closed and not runtime.lock.locked()
    assert runtime.failures["click_what"] == 1
    assert runtime.state == SessionState.FAILED


async def test_failed_backend_cleanup_preserves_retry_ownership(isolated_runtime):
    class RetryBackend:
        attempts = 0

        async def stop(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic teardown failure")

    backend = isolated_runtime.session.browser = RetryBackend()
    assert await isolated_runtime.close_backends()
    assert isolated_runtime.session.browser is backend
    assert await isolated_runtime.close_backends() == []
    assert isolated_runtime.session.browser is None


async def test_playwright_stop_failure_preserves_driver_handles():
    from argus.browser import BrowserDriver

    class FailsOnce:
        attempts = 0

        async def stop(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic Playwright failure")

    driver = BrowserDriver()
    playwright = driver._playwright = FailsOnce()
    with pytest.raises(RuntimeError):
        await driver.stop()
    assert driver._playwright is playwright
    await driver.stop()
    assert driver._playwright is None


async def test_queue_timeout_does_not_cancel_running_operation(isolated_runtime):
    runtime = isolated_runtime
    runtime.settings = replace(runtime.settings, queue_timeout=0.01)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def operation():
        entered.set()
        await release.wait()
        return "done"

    task = asyncio.create_task(runtime.execute("first", operation))
    await entered.wait()
    blocked = await runtime.execute("second", operation)
    assert blocked.error == ErrorCode.BUSY
    assert not task.done()
    release.set()
    assert (await task).value == "done"


async def test_independent_runtimes_do_not_share_state(tmp_path):
    a = create_runtime(Settings(output_dir=tmp_path / "a"))
    b = create_runtime(Settings(output_dir=tmp_path / "b"))

    async def who():
        await asyncio.sleep(0)
        return current_runtime().session.run_id

    first, second = await asyncio.gather(a.execute("who", who), b.execute("who", who))
    assert first.value != second.value
    assert a.calls["who"] == b.calls["who"] == 1


def test_event_retention_reports_gaps_and_does_not_keep_payloads():
    stream = EventStream(2)
    for _ in range(1000):
        stream.publish("operation_started", "active", "observe")
    gap, events = stream.read(0)
    assert gap and len(events) == 2 and stream.dropped == 998
    assert stream.read(999)[0] is False
    assert stream.read(2000)[0] is True
    assert set(events[0]) == {"sequence", "timestamp", "kind", "operation", "state", "code"}


def test_input_vault_and_all_report_formats_exclude_input_values(isolated_runtime, tmp_path):
    session = isolated_runtime.session
    secret = "synthetic-password-8d157"
    _record_action(session, "type_into", "Password", secret)
    assert secret not in json.dumps(session.action_trace)
    assert session.inputs.resolve(session.action_trace)[0]["value"] == secret
    bug = Bug(
        BugType.UX_ISSUE,
        Severity.HIGH,
        "Synthetic",
        "Evidence",
        "https://example.test",
        [],
        replay_steps=session.action_trace,
    )
    result = ExplorationResult("https://example.test", [bug], [], 1, 0, [])
    Reporter().generate(result, str(tmp_path))
    for path in tmp_path.glob("report_*.*"):
        assert secret not in path.read_text()
    exported = bug.to_dict()["replay_steps"]
    with pytest.raises(ValueError, match="fresh input"):
        InputVault().resolve(exported)
    session.inputs.clear()
    with pytest.raises(ValueError, match="unavailable"):
        session.inputs.resolve(session.action_trace)


@pytest.mark.parametrize(
    "values",
    [
        {"operation_timeout": 0},
        {"queue_timeout": float("nan")},
        {"monitor_port": -1},
        {"event_capacity": 0},
    ],
)
def test_settings_reject_invalid_limits(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_custom_report_exporter_has_an_explicit_extension_point(tmp_path):
    result = ExplorationResult("https://example.test", [], [], 0, 0, [])
    Reporter(exporters={"txt": lambda value: value.run_id}).generate(result, str(tmp_path))
    assert (tmp_path / f"report_{result.run_id}.txt").read_text() == result.run_id


async def test_verifier_registry_extension_and_invalid_input(isolated_runtime):
    runtime = isolated_runtime

    async def custom(session, clause, actions):
        return {"attempted": True, "reproduced": True, "method": "synthetic"}

    runtime.verifiers.register("synthetic", custom)
    receipt = await runtime.verifiers.verify(runtime.session, {"kind": "synthetic"})
    assert receipt["reproduced"] is True
    with pytest.raises(ValueError, match="already registered"):
        runtime.verifiers.register("synthetic", custom)
    invalid = await runtime.verifiers.verify(runtime.session, {"expect_status": True})
    assert invalid["attempted"] is False


async def test_exported_replay_is_rejected_before_browser_access():
    from argus.browser import BrowserDriver

    result = await BrowserDriver().replay(
        "https://example.test", [{"tool": "type_into", "requires_input": True}]
    )
    assert result["diverged"] is True
    assert "resolved in memory" in result["steps"][0]["reason"]


def test_capture_configuration_is_validated_and_per_runtime(tmp_path):
    from argus.settings import CaptureSettings

    small = create_runtime(Settings(output_dir=tmp_path, capture=CaptureSettings(network_events=2)))
    large = create_runtime(Settings(output_dir=tmp_path, capture=CaptureSettings(network_events=7)))
    assert small.browser_factory().network_log.maxlen == 2
    assert large.browser_factory().network_log.maxlen == 7
    with pytest.raises(ValueError):
        Settings.from_env({"ARGUS_MAX_NETWORK_EVENTS": "bad"})


def test_application_import_graph_has_no_cycles():
    import ast

    root = Path(__file__).resolve().parents[1] / "argus" / "application"
    graph = {}

    class RuntimeImports(ast.NodeVisitor):
        def __init__(self):
            self.imports = []

        def visit_If(self, node):
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                return
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            self.imports.append(node)

    for path in root.glob("*.py"):
        dependencies = set()
        visitor = RuntimeImports()
        visitor.visit(ast.parse(path.read_text()))
        for node in visitor.imports:
            if not node.module:
                continue
            if node.level == 1:
                dependencies.add(node.module.split(".")[0])
            elif node.module.startswith("argus.application."):
                dependencies.add(node.module.split(".")[2])
        graph[path.stem] = dependencies

    def visit(name, ancestors):
        assert name not in ancestors, f"application import cycle: {ancestors + [name]}"
        for dependency in graph.get(name, set()):
            visit(dependency, ancestors + [name])

    for name in graph:
        visit(name, [])


def test_application_does_not_access_private_backend_state():
    import ast

    root = Path(__file__).resolve().parents[1] / "argus"
    for path in (root / "application").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                receiver = ast.unparse(node.value)
                assert not receiver.endswith((".browser", ".screen")), (path, node.lineno)
    for path in (root / "domain").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not any(
                    part in (node.module or "")
                    for part in ("application", "adapters", "mcp", "playwright")
                )
    for path in (root / "application").glob("*.py"):
        assert "mcp.server" not in path.read_text()
    assert "mcp_server" not in (root / "cli.py").read_text()


def test_repository_code_does_not_use_compatibility_modules():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    shims = {"models", "detector", "reporter"}
    pattern = re.compile(r"(?:from|import)\s+argus\.(\w+)|from\s+\.(\w+)\s+import")
    offenders = []
    for path in [
        *root.glob("argus/**/*.py"),
        *root.glob("argus_lab/**/*.py"),
        *root.glob("tests/*.py"),
    ]:
        if path.parent == root / "argus" and path.stem in shims:
            continue
        for match in pattern.finditer(path.read_text()):
            module = match.group(1) or match.group(2)
            top_level = match.group(1) or path.parent == root / "argus"
            if module in shims and top_level:
                offenders.append(f"{path.relative_to(root)}: {match.group(0)}")
    assert not offenders, offenders
