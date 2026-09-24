"""Define lifecycle event kinds and typed payloads for contextualization.

Payloads are validated here before the shared pipeline event writer records them.
Events carry the stage and source identities and, when available, the run identity.
"""

from aizk.contextualization.events import (
    CONTEXTUALIZATION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    FailedPayload,
    GraphEventKind,
    QueuedPayload,
    RecoveredStalePayload,
    RequeuedPayload,
    SkippedSupersededPayload,
    SucceededPayload,
    TimedOutPayload,
)

__all__ = [
    "CONTEXTUALIZATION_STAGE",
    "CancelledPayload",
    "ClaimedPayload",
    "FailedPayload",
    "GraphEventKind",
    "QueuedPayload",
    "RecoveredStalePayload",
    "RequeuedPayload",
    "SkippedSupersededPayload",
    "SucceededPayload",
    "TimedOutPayload",
]
