"""Tests for the shared CLI boundary gates (``aizk.utilities.cli_gates``).

Every command that can initiate a corpus-wide operation reports its refusal the
same way: a plain stderr message naming the flags that resolve it, and a non-zero
exit code. A missing ``--yes`` is a usage refusal, so no traceback is printed.
``positive_int`` rejects the bound values that would silently widen a scope.
"""

from __future__ import annotations

import argparse

import pytest

from aizk.utilities.cli_gates import positive_int, refuse_unconfirmed


def test_refuse_unconfirmed_names_the_operation_and_both_resolving_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal identifies the gated operation and how to approve or scope it."""
    exit_code = refuse_unconfirmed("corpus-wide contextualization backfill", explicit_flag="--output-id")

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "corpus-wide contextualization backfill" in captured.err
    assert "large downstream blast radius" in captured.err
    assert "--yes" in captured.err
    assert "--output-id" in captured.err
    assert captured.out == "", "a refusal belongs on stderr, not in the command's output"


def test_refuse_unconfirmed_offers_dry_run_when_no_explicit_target_flag_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A command with no target-naming flag points at ``--dry-run`` instead of inventing one."""
    exit_code = refuse_unconfirmed("corpus-wide conversion backfill", explicit_flag=None)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "--dry-run" in captured.err
    assert "--source-id" not in captured.err
    assert "--output-id" not in captured.err


@pytest.mark.parametrize("value", ["1", "50", "1000"])
def test_positive_int_accepts_a_usable_bound(value: str) -> None:
    """A bound of one or more caps a scope, which is what a limit is for."""
    assert positive_int(value) == int(value)


@pytest.mark.parametrize("value", ["-1", "0", "-100"])
def test_positive_int_rejects_bounds_that_do_not_bound(value: str) -> None:
    """Zero and negatives are rejected: SQLite reads ``LIMIT -1`` as no limit at all.

    A ``--limit -1`` that reached the query would widen a bounded sweep into a
    corpus-wide one while reading like a restriction, so it is refused at the
    boundary.
    """
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        positive_int(value)


def test_positive_int_rejects_a_non_integer() -> None:
    """A non-numeric bound is a usage error, reported as argparse reports one."""
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int("many")
