# ruff: NOQA: E731
from collections import deque
import contextlib
import logging
import os

import psutil
from tqdm.auto import tqdm

logger = logging.getLogger(__name__)


class ProgressBarManager:
    """Manages progress bars for batch and non-batch execution."""

    def __init__(self, desc: str, show_progress: bool):
        self.desc = desc
        self.show_progress = show_progress

    def create_single_bar(self, total: int) -> tqdm:
        """Create a single progress bar for non-batch execution."""
        return tqdm(
            total=total,
            desc=self.desc,
            disable=not self.show_progress,
        )

    def create_nested_bars(self, total_jobs: int, batch_size: int):
        """Create nested progress bars for batch execution."""
        n_batches = (total_jobs + batch_size - 1) // batch_size

        overall_pbar = tqdm(
            total=total_jobs,
            desc=self.desc,
            disable=not self.show_progress,
            position=0,
            leave=True,
        )

        batch_pbar = tqdm(
            total=min(batch_size, total_jobs),
            desc=f"Batch 1/{n_batches}",
            disable=not self.show_progress,
            position=1,
            leave=False,
        )

        return overall_pbar, batch_pbar, n_batches

    def update_batch_bar(self, batch_pbar: tqdm, batch_num: int, n_batches: int, batch_size: int):
        """Update batch progress bar for new batch."""
        batch_pbar.reset(total=batch_size)
        batch_pbar.set_description(f"Batch {batch_num}/{n_batches}")


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
