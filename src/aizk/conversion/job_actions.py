"""The operator delete command for conversion jobs, as a domain helper.

Every operator surface — the conversion JSON API and the operator console —
calls the same domain code rather than importing another app's route
internals. The helper performs the status-eligibility check (raising
:class:`ValueError` with an operator-facing reason when ineligible) and applies
the deletion. The caller owns the surrounding ``BEGIN IMMEDIATE`` transaction;
the helper does **not** commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.conversion.datamodel.job import ConversionJob

#: Statuses a delete may remove from.
DELETABLE_STATUSES = frozenset(
    {
        WorkUnitStatus.FAILED,
        WorkUnitStatus.CANCELLED,
        WorkUnitStatus.TIMED_OUT,
    }
)


def apply_job_delete(session: "Session", job: "ConversionJob") -> None:
    """Delete a terminal conversion job and its output row.

    Raises :class:`ValueError` (``"job_not_deletable"``) when the job is not in a
    deletable terminal status. This is a hard delete, not a lifecycle transition:
    it removes the job's :class:`ConversionOutput` (when present) and the job row
    itself, recording no event. The job's prior lifecycle events are left intact.
    """
    if job.status not in DELETABLE_STATUSES:
        raise ValueError("job_not_deletable")
    output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).first()
    if output:
        session.delete(output)
    session.delete(job)
