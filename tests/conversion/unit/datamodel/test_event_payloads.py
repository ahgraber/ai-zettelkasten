"""Unit tests for the typed payload contract on ConversionJobEvent.

The payload contract guarantees:

- Every kind in ``ConversionEventKind`` has a corresponding pydantic model
  with ``extra="forbid"`` on write — typos and stale fields surface as
  ``ValidationError`` at insertion, not at read time.
- Write-time validation is exhaustive: an unknown kind, or an extra field
  on a known kind, both raise.
- Read-time deserialization is lenient: unrecognized fields on a previously
  persisted row are ignored so additive payload changes are forward
  compatible.
- Round-trip serialization preserves every declared field.

The parametrization over ``ConversionEventKind`` ensures adding a new kind
without test coverage is impossible — the parametrized test would either
not run for the new kind, or raise ``KeyError`` against ``_KIND_FIXTURES``.
"""

from __future__ import annotations

import datetime
import json
from uuid import uuid4

from pydantic import BaseModel, ValidationError
import pytest

from aizk.conversion.datamodel.events import (
    CancelledPayload,
    ClaimedPayload,
    ConversionEventKind,
    FailedPayload,
    PhasePayload,
    QueuedPayload,
    RecoveredStalePayload,
    SourceEnrichedPayload,
    SucceededPayload,
    UploadPendingPayload,
    parse_payload_lenient,
)

_NOW = datetime.datetime(2026, 5, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _valid_fields(kind: ConversionEventKind) -> dict:
    """Return a valid field dict for the given kind."""
    if kind is ConversionEventKind.QUEUED:
        return {"submitted_by": "user-1", "requeue_reason": "initial"}
    if kind is ConversionEventKind.CLAIMED:
        return {"claimed_at": _NOW, "worker_pid": 1234}
    if kind is ConversionEventKind.PHASE:
        return {"phase": "preparing_input", "reported_at": _NOW}
    if kind is ConversionEventKind.CANCELLED:
        return {"cancelled_by": "user-1", "cancellation_reason": "user_request"}
    if kind is ConversionEventKind.FAILED:
        return {
            "error_code": "fetch_failed",
            "error_message": "transient timeout",
            "error_detail": "traceback...",
            "retryable": True,
            "last_phase": "preparing_input",
        }
    if kind is ConversionEventKind.SUCCEEDED:
        return {"output_id": 42, "content_hash": "deadbeef"}
    if kind is ConversionEventKind.UPLOAD_PENDING:
        return {"content_hash": "deadbeef"}
    if kind is ConversionEventKind.RECOVERED_STALE:
        return {"stale_after_minutes": 30, "last_started_at": _NOW}
    if kind is ConversionEventKind.SOURCE_ENRICHED:
        return {
            "aizk_uuid": uuid4(),
            "columns_written": ["url", "title"],
            "update_succeeded": True,
            "failure_reason": None,
        }
    raise AssertionError(f"Unhandled kind in test fixture: {kind!r}")


_KIND_TO_CLASS: dict[ConversionEventKind, type[BaseModel]] = {
    ConversionEventKind.QUEUED: QueuedPayload,
    ConversionEventKind.CLAIMED: ClaimedPayload,
    ConversionEventKind.PHASE: PhasePayload,
    ConversionEventKind.CANCELLED: CancelledPayload,
    ConversionEventKind.FAILED: FailedPayload,
    ConversionEventKind.SUCCEEDED: SucceededPayload,
    ConversionEventKind.UPLOAD_PENDING: UploadPendingPayload,
    ConversionEventKind.RECOVERED_STALE: RecoveredStalePayload,
    ConversionEventKind.SOURCE_ENRICHED: SourceEnrichedPayload,
}


# ---------------------------------------------------------------------------
# Exhaustive parametrized tests — adding a new kind without coverage breaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ConversionEventKind))
def test_valid_fields_validate(kind: ConversionEventKind) -> None:
    """Every kind accepts its declared field set."""
    model_cls = _KIND_TO_CLASS[kind]
    instance = model_cls(**_valid_fields(kind))
    assert instance.kind == kind.value


@pytest.mark.parametrize("kind", list(ConversionEventKind))
def test_extra_field_rejected_on_write(kind: ConversionEventKind) -> None:
    """``extra="forbid"`` means an unknown field on a known kind raises."""
    model_cls = _KIND_TO_CLASS[kind]
    fields = _valid_fields(kind)
    fields["this_field_does_not_exist"] = "junk"
    with pytest.raises(ValidationError):
        model_cls(**fields)


@pytest.mark.parametrize("kind", list(ConversionEventKind))
def test_round_trip_preserves_fields(kind: ConversionEventKind) -> None:
    """JSON round-trip preserves every declared field."""
    model_cls = _KIND_TO_CLASS[kind]
    fields = _valid_fields(kind)
    instance = model_cls(**fields)

    serialized = instance.model_dump_json()
    reconstructed = parse_payload_lenient(serialized)

    assert type(reconstructed) is model_cls
    assert reconstructed.model_dump(mode="json") == instance.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Discriminator and unknown-kind handling
# ---------------------------------------------------------------------------


def test_unknown_kind_rejected_on_read() -> None:
    """An unknown ``kind`` value in persisted JSON raises ``ValueError``."""
    raw = json.dumps({"kind": "not_a_real_kind", "foo": "bar"})
    with pytest.raises(ValueError, match="Unknown event kind"):
        parse_payload_lenient(raw)


def test_reader_tolerates_additive_fields() -> None:
    """Forward-compat: a row with extra fields not in the current model parses cleanly.

    Simulates a row persisted under a future code version that added an
    optional field to ``ClaimedPayload``. Current code must drop the unknown
    field rather than raise.
    """
    future_row = {
        "kind": "claimed",
        "claimed_at": _NOW.isoformat(),
        "worker_pid": 1234,
        "future_optional_field": "added later",
    }
    parsed = parse_payload_lenient(future_row)
    assert isinstance(parsed, ClaimedPayload)
    assert parsed.worker_pid == 1234
    assert parsed.claimed_at == _NOW
    # The unknown field has been dropped, not persisted as an attribute.
    assert not hasattr(parsed, "future_optional_field")
