"""Shared boundary gates for the stage CLIs: confirmation reporting and bound validation.

:func:`aizk.pipeline.invalidation.require_reprocessing_confirmation` is
surface-agnostic: it raises, and each surface decides how to tell its user.
:func:`refuse_unconfirmed` is the command-line surface's answer, shared by every
stage's CLI so a refusal reads the same whichever command produced it. A missing
``--yes`` is a usage refusal rather than a crash, so it is reported the way
argparse reports one — a plain message on stderr, no traceback — and it names the
flags that resolve it.

:func:`positive_int` guards the other direction: an operator-supplied bound that
would widen the scope it appears to narrow is refused before it reaches a query.
"""

from __future__ import annotations

import argparse
import sys

_REFUSAL = (
    "{operation} has a large downstream blast radius (cost and recomputation implications), "
    "so it requires explicit sign-off. Re-invoke with --yes to approve, or {alternative}."
)


def refuse_unconfirmed(operation: str, *, explicit_flag: str | None) -> int:
    """Report a confirmation-gate refusal on stderr and return the failure exit code.

    Args:
        operation: The gated operation, named as the gate names it (for example
            ``"corpus-wide contextualization backfill"``).
        explicit_flag: The flag that names explicit targets and so bypasses the
            gate, or ``None`` for a command that has no such flag — those point
            the operator at ``--dry-run`` instead of naming a flag that does not
            exist.

    Returns:
        The exit code the command should return.
    """
    alternative = f"name explicit targets with {explicit_flag}" if explicit_flag else "preview it with --dry-run"
    print(_REFUSAL.format(operation=operation, alternative=alternative), file=sys.stderr)
    return 1


def positive_int(value: str) -> int:
    """Parse an argparse bound, refusing values that would not bound anything.

    Intended as an argparse ``type``. Zero and negative bounds are rejected
    rather than clamped: SQLite reads ``LIMIT -1`` as "no limit", so a
    ``--limit -1`` that reached a corpus scan would enqueue the whole corpus
    while reading like a restriction.

    Args:
        value: The raw command-line token.

    Returns:
        The parsed bound.

    Raises:
        argparse.ArgumentTypeError: If ``value`` is not an integer of one or more.
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {parsed}")
    return parsed
