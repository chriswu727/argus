"""Compatibility imports; the report service lives in argus.adapters.reporting."""

from .adapters.reporting.common import _BUGTYPE_LABELS as _BUGTYPE_LABELS
from .adapters.reporting.common import _MAX_STEPS as _MAX_STEPS
from .adapters.reporting.common import _SEV_IDX as _SEV_IDX
from .adapters.reporting.common import _SEVERITY_COLORS as _SEVERITY_COLORS
from .adapters.reporting.common import _SEVERITY_ORDER as _SEVERITY_ORDER
from .adapters.reporting.common import _dedup_description as _dedup_description
from .adapters.reporting.common import _embed_image as _embed_image
from .adapters.reporting.common import _esc as _esc
from .adapters.reporting.common import _format_steps as _format_steps
from .adapters.reporting.common import _prepare_report_image as _prepare_report_image
from .adapters.reporting.common import _repro_badge as _repro_badge
from .adapters.reporting.common import _repro_detail as _repro_detail
from .adapters.reporting.common import _trust_rank as _trust_rank
from .adapters.reporting.reporter import Reporter as Reporter
