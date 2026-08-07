"""Contract tests between the ``just backfill`` recipe and the CLIs it drives.

The ``all`` target forwards one set of flags to three different commands, so a
flag any one of them does not accept breaks the whole target. Rendering the
recipe cannot catch that — the rendered script is identical whether or not the
flags parse — so these tests check the forwarded flags against each command's
real parser.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from aizk.conversion import cli as conversion_cli
from aizk.graph import cli as graph_cli

# The flags the justfile's `all` branch accepts and forwards to every leg.
# test_all_target_forwards_exactly_these_flags keeps this in step with the recipe.
_FORWARDED_FLAGS = ("--yes", "--dry-run")

_BACKFILL_LEGS = [
    pytest.param(conversion_cli, "backfill", "_cmd_backfill", id="conversion-backfill"),
    pytest.param(graph_cli, "backfill", "_cmd_backfill", id="graph-backfill"),
    pytest.param(graph_cli, "extraction-backfill", "_cmd_extraction_backfill", id="graph-extraction-backfill"),
]


def _justfile_text() -> str:
    """Return the repo-root justfile's contents."""
    return (Path(__file__).resolve().parents[1] / "justfile").read_text(encoding="utf-8")


def test_all_target_forwards_exactly_these_flags() -> None:
    """The recipe's accepted-flag list matches what these tests check against the parsers.

    Guards against justfile drift: a flag added to the ``all`` branch without
    being added here would go unchecked against the three parsers.
    """
    justfile = _justfile_text()
    # Anchor on line starts: the branch bodies nest a second `case`, whose own
    # `;;` and `*)` would otherwise terminate the scan early.
    all_branch = justfile.split("\n      all)", 1)[1].split("\n      *)", 1)[0]
    accepted = set(re.findall(r"^\s*(--[a-z-]+)\)", all_branch, flags=re.MULTILINE))

    assert accepted == set(_FORWARDED_FLAGS)


@pytest.mark.parametrize(("module", "subcommand", "command_attr"), _BACKFILL_LEGS)
def test_every_backfill_leg_accepts_the_forwarded_flags(
    module: object, subcommand: str, command_attr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each leg parses every flag the ``all`` target hands it.

    A leg whose parser rejects one of them aborts ``just backfill all`` under
    ``set -e``, so the corpus is swept partially or not at all.
    """
    monkeypatch.setattr(module, "load_process_dotenv_once", lambda: None)
    monkeypatch.setattr(module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(module, "ConversionConfig", lambda: None)
    monkeypatch.setattr(module, command_attr, lambda _args: 0)

    assert module.main([subcommand, *_FORWARDED_FLAGS]) == 0


@pytest.mark.parametrize(("module", "subcommand", "command_attr"), _BACKFILL_LEGS)
def test_no_backfill_leg_submits_work_without_confirmation(
    module: object, subcommand: str, command_attr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every leg exposes ``--yes``, so the recipe's sign-off requirement holds across all three."""
    monkeypatch.setattr(module, "load_process_dotenv_once", lambda: None)
    monkeypatch.setattr(module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(module, "ConversionConfig", lambda: None)

    captured: dict[str, object] = {}
    monkeypatch.setattr(module, command_attr, lambda args: captured.update(yes=args.yes) or 0)

    assert module.main([subcommand, "--yes"]) == 0
    assert captured["yes"] is True
