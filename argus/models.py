"""Compatibility imports; domain definitions live in argus.domain. Deprecated; removal is planned for 0.13."""

import warnings

from .domain.models import Action as Action
from .domain.models import ActionType as ActionType
from .domain.models import Bug as Bug
from .domain.models import BugType as BugType
from .domain.models import ExplorationResult as ExplorationResult
from .domain.models import InteractiveElement as InteractiveElement
from .domain.models import Observation as Observation
from .domain.models import PageState as PageState
from .domain.models import Screenshot as Screenshot
from .domain.models import Severity as Severity
from .domain.models import new_run_id as new_run_id
from .domain.models import validated_run_id as validated_run_id

warnings.warn(
    "argus.models is deprecated and will be removed in Argus 0.13; import from argus.domain.models.",
    DeprecationWarning,
    stacklevel=2,
)
