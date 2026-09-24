"""Define lifecycle event kinds and typed payloads for extraction.

Payloads are validated here before the shared pipeline event writer records them.
Events carry the stage and source identities and, when available, the run identity.
"""

from aizk.extraction.events import (
    EXTRACTION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    ExtractionEventKind,
    FailedPayload,
    QueuedPayload,
    RecoveredStalePayload,
    RequeuedPayload,
    SucceededPayload,
    TimedOutPayload,
)

__all__ = [
    "EXTRACTION_STAGE",
    "CancelledPayload",
    "ClaimedPayload",
    "ExtractionEventKind",
    "FailedPayload",
    "QueuedPayload",
    "RecoveredStalePayload",
    "RequeuedPayload",
    "SucceededPayload",
    "TimedOutPayload",
]
