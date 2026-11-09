from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import quote

import pytest

from argus.application.testgen import generate_test, run_exported_test


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/?sig=SYNTHETIC_PRIVATE",
        "https://example.test/?key=SYNTHETIC_PRIVATE",
        "https://example.test/?code=SYNTHETIC_PRIVATE&state=opaque",
        "https://example.test/?Signature=SYNTHETIC_PRIVATE",
        "https://example.test/invite/SYNTHETIC_PRIVATE",
        "https://SYNTHETIC_PRIVATE.example.test/path",
        "https://example.test/?author=SYNTHETIC_PRIVATE&side=left",
        "https://user:SYNTHETIC_PRIVATE@example.test/#SYNTHETIC_PRIVATE",
    ],
)
def test_application_strings_never_enter_export_channels(url):
    private = "SYNTHETIC_PRIVATE"
    secrets, required = {}, {}
    source, notes = generate_test(
        finding_id=private,
        title=f"Broken at {url}",
        expected=f"Keep {private}",
        receipt={"expect": "present", "target_text": private, "at_url": url, "runs": private},
        journey=[
            {"tool": "navigate", "value": url},
            {
                "tool": "type_into",
                "locator": f"page.get_by_label({private!r})",
                "description": private,
            },
            {"tool": "select_into", "locator": f"page.locator({private!r})"},
            {"tool": "hover_what", "value": f"text:{private}"},
            {"tool": "press_key", "key": private},
            {"tool": "upload_file", "description": url},
            {"tool": "capsule_restore", "description": private},
            {"tool": private, "description": url},
        ],
        start_url=url,
        resolve_input=lambda action: private,
        filename=private + ".py",
        secrets=secrets,
        required_env=required,
    )
    artifact = source + json.dumps(notes) + json.dumps(required)
    assert private not in artifact
    assert url not in artifact
    assert set(required) == set(secrets)
    assert required["ARGUS_URL_1"] == (
        "initial navigation URL; step 1 navigation URL; fresh-load verification URL"
    )
    assert secrets["ARGUS_URL_1"] == url
    assert secrets["ARGUS_INPUT_1"] == private
    compile(source, "regression.py", "exec")


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_subprocess_output_is_never_reflected(tmp_path, channel):
    private = "synthetic-private /?value"
    script = tmp_path / "failure.py"
    script.write_text(
        f"import sys\nprint({(private + quote(private, safe=''))!r}, file=sys.{channel})\n"
        "sys.exit(1)\n"
    )
    result = await run_exported_test(script, 10, secrets={"ARGUS_URL_1": private})
    assert result["outcome"] == "errored"
    assert private not in json.dumps(result)
    assert quote(private, safe="") not in json.dumps(result)


async def test_real_browser_navigation_errors_hide_url_in_tool_and_standalone_output(tmp_path):
    private = "SYNTHETIC_NAV_ERROR"
    secrets = {}
    source, notes = generate_test(
        finding_id="run:1",
        title=private,
        expected=private,
        receipt={"expect": "present", "target_text": private},
        journey=[],
        start_url=f"http://127.0.0.1:9/invite/{private}?code={private}",
        resolve_input=lambda action: None,
        filename="regression.py",
        secrets=secrets,
    )
    assert not notes
    path = tmp_path / "test_regression.py"
    path.write_text(source)
    result = await run_exported_test(path, 30, secrets=secrets)
    assert result["outcome"] == "errored"
    assert private not in json.dumps(result)
    for command in (
        [sys.executable, str(path)],
        [sys.executable, "-m", "pytest", "-q", "--showlocals", str(path)],
    ):
        process = subprocess.run(
            command,
            env={**os.environ, **secrets},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert process.returncode != 0
        assert private not in process.stdout + process.stderr
        assert "ARGUS_EXECUTION_ERROR" in process.stdout + process.stderr


def test_missing_input_is_incomplete_not_replayed_as_empty():
    source, notes = generate_test(
        finding_id="run:1",
        title="",
        expected="",
        receipt={"expect": "present", "target_text": "x"},
        journey=[{"tool": "type_into", "locator": 'page.locator("#input")'}],
        start_url="http://example.test/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    assert notes == ["step 1: input value unavailable; supply it before running"]
    assert "ARGUS_INCOMPLETE_TEST" in source


@pytest.mark.parametrize("save_values", [False, True])
async def test_tool_result_filename_and_manual_steps_do_not_echo_application_data(
    tmp_path, monkeypatch, save_values
):
    from types import SimpleNamespace

    from argus.application import exports

    private = "SYNTHETIC_TOOL_PRIVATE"
    url = f"https://example.test/invite/{private}?sig={private}"
    bug = SimpleNamespace(
        title=private,
        expected=private,
        journey_end=1,
        reproduction_receipt={
            "reproduced": True,
            "expect": "present",
            "target_text": private,
            "at_url": url,
        },
    )
    bug.verification_spec = {**bug.reproduction_receipt, "version": 1}
    session = SimpleNamespace(
        bugs=[bug],
        run_id="synthetic-run",
        url=url,
        steps=[],
        action_trace=[{"tool": "upload_file", "description": private}],
        capsule_restores=[],
    )
    monkeypatch.setattr(exports, "_require_session", lambda: session)
    monkeypatch.setattr(exports, "_require_web_session", lambda *args: None)
    monkeypatch.setattr(
        exports,
        "current_runtime",
        lambda: SimpleNamespace(settings=SimpleNamespace(output_dir=tmp_path)),
    )
    result = await exports.export_test("synthetic-run:1", run=True, save_private_values=save_values)
    assert result.data["run"]["outcome"] == "incomplete"
    assert (
        result.data["required_env"]["ARGUS_URL_1"]
        == "initial navigation URL; fresh-load verification URL"
    )
    from pathlib import Path

    source = Path(result.data["path"]).read_text()
    assert private not in source + str(result) + json.dumps(result.data)
    assert Path(result.data["path"]).name == "test_argus_synthetic-run_1.py"

    if save_values:
        values_path = Path(result.data["private_values_path"])
        assert json.loads(values_path.read_text())["values"]["ARGUS_URL_1"] == url
        assert values_path.parent.joinpath(".gitignore").read_text() == "*\n"
        if os.name == "posix":
            assert values_path.stat().st_mode & 0o777 == 0o600
            assert values_path.parent.stat().st_mode & 0o777 == 0o700
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        ignored = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", str(values_path)], capture_output=True
        )
        assert ignored.returncode == 0
    else:
        assert result.data["private_values_path"] is None
        assert not list(tmp_path.rglob("*.json"))


def test_private_values_file_is_opt_in_scoped_and_environment_overrides_it(tmp_path, monkeypatch):
    source, _ = generate_test(
        finding_id="run:1",
        title="",
        expected="",
        receipt={"expect": "present", "target_text": "private-text"},
        journey=[],
        start_url="https://original.test/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    values_path = tmp_path / "values.json"
    values_path.write_text(
        json.dumps(
            {
                "ARGUS_URL_1": "https://saved.test/",
                "ARGUS_TEXT_1": "private-text",
                "PATH": "untrusted",
            }
        )
    )
    namespace = {"__name__": "generated"}
    exec(compile(source, "t.py", "exec"), namespace)
    payload = {"export_id": namespace["EXPORT_ID"], "values": json.loads(values_path.read_text())}
    values_path.write_text(json.dumps(payload))
    monkeypatch.setenv("ARGUS_VALUES_FILE", str(values_path))
    monkeypatch.setenv("ARGUS_EXPORT_ID", namespace["EXPORT_ID"])
    monkeypatch.setenv("ARGUS_URL_1", "https://override.test/")
    monkeypatch.delenv("ARGUS_TEXT_1", raising=False)
    values = namespace["_values"]()
    assert values == {"ARGUS_URL_1": "https://override.test/", "ARGUS_TEXT_1": "private-text"}
    assert "ARGUS_TEXT_1" not in os.environ
    assert os.environ["PATH"] != "untrusted"


@pytest.mark.parametrize(
    "saved,detail",
    [
        ("SYNTHETIC_PRIVATE_BROKEN_JSON", "could not be read"),
        ('{"ARGUS_URL_1": 42}', "values must be strings"),
        ('{"ARGUS_URL_1":"/relative", "ARGUS_TEXT_1":"SYNTHETIC_PRIVATE"}', "absolute http(s) URL"),
    ],
)
async def test_invalid_private_values_fail_with_safe_diagnostics(
    tmp_path, monkeypatch, saved, detail
):
    source, _ = generate_test(
        finding_id="run:1",
        title="",
        expected="",
        receipt={"expect": "present", "target_text": "x"},
        journey=[],
        start_url="http://example.test/",
        resolve_input=lambda action: None,
        filename="t.py",
    )
    path = tmp_path / "t.py"
    path.write_text(source)
    values_path = tmp_path / "private.json"
    namespace = {"__name__": "generated"}
    exec(compile(source, "t.py", "exec"), namespace)
    try:
        saved = json.dumps({"export_id": namespace["EXPORT_ID"], "values": json.loads(saved)})
    except json.JSONDecodeError:
        pass
    values_path.write_text(saved)
    monkeypatch.setenv("ARGUS_VALUES_FILE", str(values_path))
    monkeypatch.delenv("ARGUS_URL_1", raising=False)
    result = await run_exported_test(path, 10)
    assert result["outcome"] == "errored"
    assert detail in result["detail"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)


async def test_unrepresentable_environment_does_not_expose_value(tmp_path):
    path = tmp_path / "t.py"
    path.write_text("raise AssertionError('must not run')")
    result = await run_exported_test(path, 10, secrets={"ARGUS_INPUT_1": "SYNTHETIC_PRIVATE\x00"})
    assert result["outcome"] == "errored"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)


async def test_failed_private_values_write_cleans_up_and_returns_fixed_error(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from argus.application import exports
    from argus.storage import StorageError

    bug = SimpleNamespace(
        title="private",
        expected="private",
        journey_end=0,
        reproduction_receipt={"reproduced": True, "expect": "present", "target_text": "private"},
    )
    bug.verification_spec = {**bug.reproduction_receipt, "version": 1}
    session = SimpleNamespace(
        bugs=[bug],
        run_id="run",
        url="https://example.test/",
        steps=[],
        action_trace=[],
        capsule_restores=[],
    )
    monkeypatch.setattr(exports, "_require_session", lambda: session)
    monkeypatch.setattr(exports, "_require_web_session", lambda *args: None)
    monkeypatch.setattr(
        exports,
        "current_runtime",
        lambda: SimpleNamespace(settings=SimpleNamespace(output_dir=tmp_path)),
    )
    write = exports.atomic_write_text

    def fail_values(path, text):
        if path.suffix == ".json":
            raise StorageError("SYNTHETIC_PRIVATE_WRITE_FAILURE")
        write(path, text)

    monkeypatch.setattr(exports, "atomic_write_text", fail_values)
    result = await exports.export_test("run:1", save_private_values=True)
    assert "could not save the export" in result
    assert "SYNTHETIC_PRIVATE" not in result
    assert not list(tmp_path.rglob("*.py"))
    assert not list(tmp_path.rglob(".argus-values-*"))
