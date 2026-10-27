"""Compatibility imports; domain definitions live in argus.domain. Deprecated; removal is planned for 0.13."""

import warnings

from .domain.detector import Detector as Detector

warnings.warn(
    "argus.detector is deprecated and will be removed in Argus 0.13; import from argus.domain.detector.",
    DeprecationWarning,
    stacklevel=2,
)
