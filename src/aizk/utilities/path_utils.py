import logging
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Annotated, List

from pydantic import AfterValidator, BeforeValidator, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)


def get_repo_path(file: str | Path) -> Path:
    """Identify repo path with git."""
    import subprocess

    repo = subprocess.check_output(  # NOQA: S603
        ["git", "rev-parse", "--show-toplevel"],  # NOQA: S607
        cwd=Path(file).parent,
        encoding="utf-8",
    ).strip()

    repo = Path(repo).expanduser().resolve()
    return repo


def get_project_path(file: str | Path) -> Path:
    """Return the nearest project root containing ``pyproject.toml``."""
    start = Path(file).expanduser().resolve()
    current = start if start.is_dir() else start.parent
    repo_root = get_repo_path(start)

    while True:
        candidate = current / "pyproject.toml"
        if candidate.exists():
            return current

        if current == repo_root:
            break
        if current.parent == current:
            break
        current = current.parent

    raise FileNotFoundError(f"No pyproject.toml found within repository {repo_root} starting from: {start}")


def path_is_valid(path: Path | str) -> Path:
    """Check whether full path can be resolved."""
    path = Path(path) if isinstance(path, str) else path
    path = path.expanduser().absolute()  # resolve ~/ -> /home/<username/ and ../../
    _ = path.resolve()  # make sure symlinks can be resolved, but dont return resolved link
    return path


def path_is_dir(path: Path | str) -> Path:
    """Test whether path is dir."""
    path = path_is_valid(path)
    if os.path.isdir(path):
        if os.access(path, os.R_OK):
            return path
        else:
            raise PermissionError(f"Path is not readable: {path}")
    else:
        raise NotADirectoryError(f"Path is not a directory: {path}")


DirPath = Annotated[Path, AfterValidator(path_is_dir)]


def path_is_file(path: Path | str) -> Path:
    """Test whether path is file."""
    path = path_is_valid(path)
    if os.path.isfile(path):
        if os.access(path, os.R_OK):
            return path
        else:
            raise PermissionError(f"Path is not readable: {path}")
    else:
        raise FileNotFoundError(f"Path is not a file: {path}")


FilePath = Annotated[Path, AfterValidator(path_is_file)]
