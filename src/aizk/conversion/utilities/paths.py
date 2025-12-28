"""Path helpers for conversion artifacts."""

from __future__ import annotations

from pathlib import Path

OUTPUT_MARKDOWN_FILENAME = "output.md"
METADATA_FILENAME = "metadata.json"
FIGURES_DIRNAME = "figures"


def metadata_path(workspace: Path) -> Path:
    """Return the path to the metadata JSON file."""
    return workspace / METADATA_FILENAME


def markdown_path(workspace: Path, filename: str = OUTPUT_MARKDOWN_FILENAME) -> Path:
    """Return the path to the markdown artifact."""
    return workspace / filename


def figure_dir(workspace: Path) -> Path:
    """Return the path to the figures directory."""
    return workspace / FIGURES_DIRNAME


def figure_paths(workspace: Path, figure_files: list[str]) -> list[Path]:
    """Return figure paths rooted in the workspace figures directory."""
    return [figure_dir(workspace) / name for name in figure_files]
