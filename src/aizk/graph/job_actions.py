"""Apply operator retry, cancel, and extraction-readmission transitions.

Each helper drives its stage's :class:`~aizk.pipeline.repository.WorkUnitRepository`,
which validates status eligibility, mutates the work-unit, and stages the
matching lifecycle event. The caller owns the surrounding transaction; these
helpers do not commit. An ineligible status raises
:class:`~aizk.pipeline.repository.IneligibleTransitionError`, a ``ValueError``
whose message is the text the operator surfaces return.
"""

from aizk.contextualization.job_actions import apply_contextualization_cancel, apply_contextualization_retry
from aizk.extraction.job_actions import apply_extraction_cancel, apply_extraction_readmission, apply_extraction_retry
from aizk.pipeline.repository import CANCELLABLE_FROM, RETRYABLE_FROM

__all__ = [
    "CANCELLABLE_FROM",
    "RETRYABLE_FROM",
    "apply_contextualization_cancel",
    "apply_contextualization_retry",
    "apply_extraction_cancel",
    "apply_extraction_readmission",
    "apply_extraction_retry",
]
