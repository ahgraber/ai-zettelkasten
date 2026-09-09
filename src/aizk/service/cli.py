"""Shared command-line surface for the stage CLIs: the process preamble and boundary gates.

:func:`prepare_process` is the preamble every ``main`` runs before it parses
arguments, so a command's first emission already goes through the structured
formatter and any model call it makes is traced the same way in every process.

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

from aizk.service.bootstrap import load_process_dotenv_once
from aizk.service.logging import configure_logging
from aizk.service.settings import ServiceSettings
from aizk.utilities.tracing import configure_mlflow_tracing

_REFUSAL = (
    "{operation} has a large downstream blast radius (cost and recomputation implications), "
    "so it requires explicit sign-off. Re-invoke with --yes to approve, or {alternative}."
)


def prepare_process() -> ServiceSettings:
    """Run the process preamble shared by every stage CLI and return its settings.

    Loads ``.env`` into the process environment first, so the settings the
    preamble reads and every settings class a command constructs afterwards see
    the same environment. Logging is configured before any command runs — without
    it a command would emit through Python's ``lastResort`` handler (stderr-only,
    default format), silently dropping the forensic ``extra`` keys the
    network-egress audit trail depends on. Tracing is process-global, so it is
    configured here beside logging rather than injected into each model client.

    Returns:
        The settings the preamble read, so a command can reuse them instead of
        re-reading the environment.
    """
    load_process_dotenv_once()
    settings = ServiceSettings()
    configure_logging(settings)
    configure_mlflow_tracing(settings.mlflow)
    return settings


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
