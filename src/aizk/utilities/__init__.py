from pathlib import Path

from .limiters import AsyncTimeWindowRateLimiter, TimeWindowRateLimiter
from .parse import URL_REGEX, detect_encoding, extract_json, extract_url, find_all_urls, validate_url
from .path_helpers import find_binary_abspath, path_is_dir, path_is_executable, path_is_file, path_is_valid
from .process import run_

__all__ = [
    "URL_REGEX",
    "detect_encoding",
    "extract_json",
    "extract_url",
    "find_all_urls",
    "validate_url",
    "find_binary_abspath",
    "path_is_dir",
    "path_is_executable",
    "path_is_file",
    "path_is_valid",
    "AsyncTimeWindowRateLimiter",
    "TimeWindowRateLimiter",
    "run_",
]

LOG_FMT = "%(asctime)s - %(levelname)-8s - %(name)s - %(funcName)s:%(lineno)d - %(message)s"


def basic_log_config() -> None:
    """Configure logging defaults."""
    import logging

    logging.basicConfig(format=LOG_FMT)


def get_repo_path(file: str | Path) -> Path:
    import subprocess

    repo = subprocess.check_output(  # NOQA: S603
        ["git", "rev-parse", "--show-toplevel"],  # NOQA: S607
        cwd=Path(file).parent,
        encoding="utf-8",
    ).strip()

    repo = Path(repo).expanduser().resolve()
    return repo
