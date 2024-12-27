# ruff: NOQA: E731
from collections import deque
from functools import wraps
import logging
import os
import signal
from subprocess import PIPE, CalledProcessError, CompletedProcess, Popen, TimeoutExpired, _mswindows
import sys
import time

logger = logging.getLogger(__name__)


def run(
    *popenargs,
    input=None,  # NOQA: A002
    capture_output=True,
    timeout=None,
    check=False,
    text=False,
    start_new_session=True,
    **kwargs,
):
    """Patched replica of subprocess.run to kill forked child subprocesses and fix blocking io making 'timeout' ineffective.

    Ref: https://github.com/ArchiveBox/ArchiveBox/blob/v0.8.6rc0/archivebox/misc/system.py
    Ref: https://github.com/python/cpython/blob/main/Lib/subprocess.py
    """
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = PIPE

    if capture_output:
        if ("stdout" in kwargs) or ("stderr" in kwargs):
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE

    pgid = None
    try:
        with Popen(*popenargs, start_new_session=start_new_session, text=text, **kwargs) as process:  # NOQA: S603
            pgid = os.getpgid(process.pid)
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except TimeoutExpired as exc:
                process.kill()
                if _mswindows:
                    # Windows accumulates the output in a single blocking
                    # read() call run on child threads, with the timeout
                    # being done in a join() on those threads.  communicate()
                    # _after_ kill() is required to collect that and add it
                    # to the exception.
                    exc.stdout, exc.stderr = process.communicate()
                else:
                    # POSIX _communicate already populated the output so
                    # far into the TimeoutExpired exception.
                    process.wait()
                raise
            except:  # Including KeyboardInterrupt, communicate handled that.
                process.kill()
                # We don't call process.wait() as .__exit__ does that for us.
                raise

            retcode = process.poll()
            if check and retcode:
                raise CalledProcessError(retcode, process.args, output=stdout, stderr=stderr)
    finally:
        # force kill any straggler subprocesses that were forked from the main proc
        try:
            os.killpg(pgid, signal.SIGINT)
        except Exception as e:
            logger.debug(e)
            pass

    return CompletedProcess(process.args, retcode, stdout, stderr)


class TimeWindowRateLimiter:
    """Rate limiter that allows a maximum number of actions over sliding time window (seconds).

    If the maximum number of actions occurs in the time window, the rate limiter will block until a slot becomes available.
    This is not an async limiter; it assumes that actions are synchronous and blocking.
    Therefore, if an action takes a long time to complete, it will block subsequent actions from starting until it completes _even if it exceeds the window period_.
    """

    def __init__(self, max_actions: int, window_seconds: int, min_interval: float = 0.1):
        if max_actions > 0:
            self.max_actions = max_actions
        else:
            raise ValueError("max_actions must be > 0")

        if window_seconds > 0:
            self.window_seconds = window_seconds
        else:
            raise ValueError("window_seconds must be > 0")

        if min_interval >= 0:
            self.min_interval = min_interval
        else:
            raise ValueError("min_interval must be >= 0")

        self.start_times = deque()
        self.n_active = 0
        self.last = time.monotonic()

    def _update(self):
        """Remove actions should no longer be blocking the queue.

        An action may take longer to execute than the time period we track.
        This action counts against the limit until its start time exits the window.
        Once its start time exits the window, its slot becomes available for new operations, while the action itself continues running to completion.
        """
        window_start = time.monotonic() - self.window_seconds

        while self.start_times and self.start_times[0] < window_start:
            self.start_times.popleft()
            if self.n_active > 0:
                logging.debug("Sliding window freed a slot")
                self.n_active -= 1

    def _wait(self):
        """Wait until a slot is available."""
        while True:
            self._update()
            if len(self.start_times) < self.max_actions:
                self.start_times.append(time.monotonic())
                self.n_active += 1
                break

            if self.start_times:
                wait_time = max(self.min_interval, self.start_times[0] + self.window_seconds - time.monotonic())
                if wait_time > 0:
                    logging.debug(f"Waiting {wait_time:.2f} seconds")
                    time.sleep(wait_time)

    def _complete(self):
        """Remove completed action from active count."""
        logging.debug("Completing action")
        self.n_active -= 1

    def __call__(self, func):
        """Apply rate limiting to a function as a decorator."""

        # TODO: add support for async functions?
        @wraps(func)
        def wrapped(*args, **kwargs):
            self._wait()
            try:
                return func(*args, **kwargs)
            finally:
                self._complete()

        return wrapped
