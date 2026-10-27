from __future__ import annotations

import importlib
import sys

import pytest


@pytest.mark.parametrize(
    "module,replacement",
    [
        ("argus.models", "argus.domain.models"),
        ("argus.detector", "argus.domain.detector"),
        ("argus.reporter", "argus.adapters.reporting"),
    ],
)
def test_compatibility_modules_warn_and_still_re_export(module, replacement):
    sys.modules.pop(module, None)
    with pytest.warns(DeprecationWarning, match=f"{module} is deprecated.*{replacement}"):
        imported = importlib.import_module(module)
    assert imported.__doc__ and "removal is planned for 0.13" in imported.__doc__


def test_re_exports_are_the_canonical_objects():
    from argus.adapters.reporting.reporter import Reporter
    from argus.domain.detector import Detector
    from argus.domain.models import Bug

    with pytest.warns(DeprecationWarning):
        for name in ("argus.models", "argus.detector", "argus.reporter"):
            sys.modules.pop(name, None)
            importlib.import_module(name)
    assert sys.modules["argus.models"].Bug is Bug
    assert sys.modules["argus.detector"].Detector is Detector
    assert sys.modules["argus.reporter"].Reporter is Reporter
