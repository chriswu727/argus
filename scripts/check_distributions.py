from __future__ import annotations

import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PUBLIC_FILES = {
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "PRIVACY.md",
    "AGENTS.md",
    "llms-install.md",
    "server.json",
    "glama.json",
    "docs/architecture.md",
    "docs/decisions.md",
    "docs/implementation-0.7.md",
    "docs/agent-protocol.md",
    "docs/evaluation.md",
    "docs/evaluation-0.8.md",
    "scripts/check_distributions.py",
    "test-site/app.py",
    "human-eye-fixture/app.py",
    "PKG-INFO",
}


def allowed(path: str, *, wheel: bool) -> bool:
    if not wheel and path == ".gitignore":
        return True
    name = PurePosixPath(path)
    if not name.parts or name.is_absolute() or any(part.startswith(".") for part in name.parts):
        return False
    if wheel and len(name.parts) > 1 and name.parts[0].endswith(".dist-info"):
        return name.name in {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "LICENSE"}
    if name.parts[0] == "argus" and name.suffix in {".py", ".js", ".html", ".css"}:
        return True
    if wheel:
        return False
    if name.parts[0] == "argus_lab" and name.suffix == ".py":
        return True
    return (
        path in PUBLIC_FILES
        or (name.parts[0] == "tests" and name.suffix == ".py")
        or (name.parts[0] == "examples" and name.suffix in {".json", ".yaml"})
    )


def check(archive_path: Path) -> None:
    if archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as archive:
            items = [item for item in archive.infolist() if not item.is_dir()]
        invalid = [
            item.filename
            for item in items
            if stat.S_ISLNK(item.external_attr >> 16) or not allowed(item.filename, wheel=True)
        ]
    elif archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path) as archive:
            items = [item for item in archive if not item.isdir()]
        invalid = [
            item.name
            for item in items
            if not item.isfile()
            or len(PurePosixPath(item.name).parts) < 2
            or PurePosixPath(item.name).parts[0] != archive_path.name.removesuffix(".tar.gz")
            or not allowed(str(PurePosixPath(*PurePosixPath(item.name).parts[1:])), wheel=False)
        ]
    else:
        raise ValueError(f"Unsupported distribution: {archive_path.name}")
    if invalid:
        raise ValueError(f"Unexpected archive entries in {archive_path.name}: {invalid}")
    print(f"Public content check passed: {archive_path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: check_distributions.py ARCHIVE [ARCHIVE ...]")
    for argument in sys.argv[1:]:
        check(Path(argument))
