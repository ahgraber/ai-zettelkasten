"""Tests for the generic work-unit lifecycle and retry classification.

Covers the spec requirements that every work-unit reaches exactly one classified
terminal outcome and that only retryable terminal failures are retry-eligible.
"""

from __future__ import annotations

import pytest

from aizk.pipeline.lifecycle import (
    RetryClass,
    TerminalOutcome,
    WorkUnitStatus,
    is_terminal,
)

_TERMINAL = [
    WorkUnitStatus.SUCCEEDED,
    WorkUnitStatus.FAILED,
    WorkUnitStatus.CANCELLED,
    WorkUnitStatus.TIMED_OUT,
]
_NON_TERMINAL = [WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING]


@pytest.mark.parametrize("status", _TERMINAL)
def test_terminal_statuses_are_terminal(status: WorkUnitStatus) -> None:
    """The four outcome states are terminal."""
    assert is_terminal(status) is True


@pytest.mark.parametrize("status", _NON_TERMINAL)
def test_non_terminal_statuses_are_not_terminal(status: WorkUnitStatus) -> None:
    """Queued and running are not terminal."""
    assert is_terminal(status) is False


def test_single_terminal_outcome_classified() -> None:
    """Each terminal outcome holds exactly one status; failed ones are classified.

    A succeeded, cancelled, or timed-out outcome carries no retry class; a failed
    outcome must be classified retryable or permanent.
    """
    succeeded = TerminalOutcome(WorkUnitStatus.SUCCEEDED)
    cancelled = TerminalOutcome(WorkUnitStatus.CANCELLED)
    timed_out = TerminalOutcome(WorkUnitStatus.TIMED_OUT)
    failed_retryable = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
    failed_permanent = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)

    assert succeeded.status is WorkUnitStatus.SUCCEEDED and succeeded.retry_class is None
    assert cancelled.status is WorkUnitStatus.CANCELLED and cancelled.retry_class is None
    assert timed_out.status is WorkUnitStatus.TIMED_OUT and timed_out.retry_class is None
    assert failed_retryable.retry_class is RetryClass.RETRYABLE
    assert failed_permanent.retry_class is RetryClass.PERMANENT


def test_failed_outcome_requires_classification() -> None:
    """A failed outcome with no retry class is rejected."""
    with pytest.raises(ValueError, match="classified retryable or permanent"):
        TerminalOutcome(WorkUnitStatus.FAILED)


@pytest.mark.parametrize("status", [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT])
def test_non_failed_outcome_rejects_classification(status: WorkUnitStatus) -> None:
    """Only a failed outcome may carry a retry class."""
    with pytest.raises(ValueError, match="only meaningful for a failed outcome"):
        TerminalOutcome(status, RetryClass.RETRYABLE)


@pytest.mark.parametrize("status", _NON_TERMINAL)
def test_non_terminal_status_is_not_a_terminal_outcome(status: WorkUnitStatus) -> None:
    """A non-terminal status cannot form a terminal outcome."""
    with pytest.raises(ValueError, match="not a terminal outcome"):
        TerminalOutcome(status)


def test_only_retryable_eligible() -> None:
    """A retryable-failed outcome is retry-eligible; a permanent-failed one is not."""
    retryable = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
    permanent = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)

    assert retryable.is_retryable is True
    assert permanent.is_retryable is False


@pytest.mark.parametrize("status", [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT])
def test_non_failed_terminal_outcomes_are_not_retryable(status: WorkUnitStatus) -> None:
    """Succeeded, cancelled, and timed-out outcomes are never retry-eligible."""
    assert TerminalOutcome(status).is_retryable is False
