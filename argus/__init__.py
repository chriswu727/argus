"""Argus — AI-powered exploratory QA agent."""
from pathlib import Path


def _resolve_version() -> str:
    """Single source of truth for the Argus version.

    Source checkout / editable install: read pyproject.toml on disk, because
    pip's recorded metadata is stamped at install time and lags a local
    version bump. Wheel install (no pyproject beside the package): fall back
    to the recorded package metadata.
    """
    try:
        pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pp.exists():
            for line in pp.read_text().splitlines():
                key, sep, val = line.partition("=")
                if sep and key.strip() == "version" and val.strip():
                    return val.strip().strip('"').strip("'")
    except Exception:
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("argus-testing")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    return "0.0.0+unknown"


__version__ = _resolve_version()
