"""In-process stand-ins for the spawn multiprocessing context."""

from __future__ import annotations

from collections.abc import Callable
import queue


class InlineProcess:
    """A Process that runs its target immediately in the calling process."""

    pid = None

    def __init__(self, target: Callable, args: tuple) -> None:
        """Hold the target and its arguments until ``start``."""
        self._target = target
        self._args = args
        self.exitcode: int | None = None

    def start(self) -> None:
        """Run the target now; the exit code is 0, or 1 when it raises."""
        try:
            self._target(*self._args)
            self.exitcode = 0
        except Exception:
            self.exitcode = 1

    def is_alive(self) -> bool:
        """Return False: the target has already run to completion."""
        return False

    def join(self, timeout: float | None = None) -> None:
        """Return at once; nothing runs in the background."""
        return

    def terminate(self) -> None:
        """Return at once; there is no process to signal."""
        return

    def kill(self) -> None:
        """Return at once; there is no process to signal."""
        return


class InlineContext:
    """A multiprocessing context whose Process runs inline and whose Queue is a plain queue."""

    def Queue(self) -> queue.Queue:  # noqa: N802
        """Return a plain in-process queue."""
        return queue.Queue()

    def Process(self, target: Callable, args: tuple, daemon: bool) -> InlineProcess:  # noqa: N802
        """Return an InlineProcess for ``target``; ``daemon`` is accepted and ignored."""
        return InlineProcess(target, args)
