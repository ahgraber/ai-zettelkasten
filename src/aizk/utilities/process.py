# ruff: NOQA: E731
import contextlib
import logging
import os
from typing import (
    Any,
    Callable,
    Optional,
)

import psutil
from tqdm.auto import tqdm

import tenacity

logger = logging.getLogger(__name__)


# %%
@contextlib.contextmanager
def temp_env_var(key, value):
    """Context manager to temporarily set an environment variable."""
    original_value = os.getenv(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if original_value is not None:
            os.environ[key] = original_value
        else:
            del os.environ[key]


@contextlib.contextmanager
def process_manager(name: str):
    """Context manager to manage processes.

    Useful if calling code spawns new processes and you want to ensure they are killed when done.
    """
    # Store existing processes to protect them
    existing = {p.pid for p in psutil.process_iter(["name"]) if name in p.info["name"].lower()}

    try:
        yield
    finally:
        # Kill new processes only
        for proc in psutil.process_iter(["name"]):
            if name in proc.info["name"].lower() and proc.pid not in existing:
                proc.kill()
