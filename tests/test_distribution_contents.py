import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_distributions.py"))


@pytest.mark.parametrize("name", ["", "docs/local-notes.md", ".env", "argus/.env", "../README.md"])
def test_unlisted_content_is_rejected(name):
    assert not CHECK["allowed"](name, wheel=False)
    assert not CHECK["allowed"](name, wheel=True)


def test_source_archive_accepts_product_files(tmp_path):
    path = tmp_path / "argus_testing-1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo("argus_testing-1.0/docs/architecture.md")
        entry.size = 2
        archive.addfile(entry, io.BytesIO(b"ok"))
    CHECK["check"](path)


def test_lab_code_is_allowed_only_in_source_distribution():
    assert CHECK["allowed"]("argus_lab/bench/runner.py", wheel=False)
    assert not CHECK["allowed"]("argus_lab/bench/runner.py", wheel=True)


def test_translation_is_allowed_only_in_source_distribution():
    assert CHECK["allowed"]("README.zh-CN.md", wheel=False)
    assert not CHECK["allowed"]("README.zh-CN.md", wheel=True)


@pytest.mark.parametrize("name", ["../README.md", "argus_testing-1.0/docs/local-notes.md"])
def test_source_archive_rejects_unexpected_entries(tmp_path, name):
    path = tmp_path / "argus_testing-1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = 2
        archive.addfile(entry, io.BytesIO(b"ok"))
    with pytest.raises(ValueError, match="Unexpected archive entries"):
        CHECK["check"](path)


def test_source_archive_rejects_links(tmp_path):
    path = tmp_path / "argus_testing-1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo("argus_testing-1.0/README.md")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "../../local-notes.md"
        archive.addfile(entry)
    with pytest.raises(ValueError, match="Unexpected archive entries"):
        CHECK["check"](path)


def test_wheel_rejects_workspace_files(tmp_path):
    path = tmp_path / "argus_testing-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("argus/__init__.py", "")
        archive.writestr("docs/local-notes.md", "local")
    with pytest.raises(ValueError, match="Unexpected archive entries"):
        CHECK["check"](path)


def test_wheel_accepts_code_resources_and_metadata(tmp_path):
    path = tmp_path / "argus_testing-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("argus/__init__.py", "")
        archive.writestr("argus/adapters/assets/dashboard.js", "")
        archive.writestr("argus_testing-1.0.dist-info/METADATA", "")
    CHECK["check"](path)


def test_source_control_config_is_only_allowed_at_archive_root():
    assert CHECK["allowed"](".gitignore", wheel=False)
    assert not CHECK["allowed"]("argus/.gitignore", wheel=False)
    assert not CHECK["allowed"](".gitignore", wheel=True)
