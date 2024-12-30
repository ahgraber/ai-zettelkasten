# ruff: NOQA: E731
from collections import deque
from functools import wraps
import logging
import os
import signal
from subprocess import PIPE, CalledProcessError, CompletedProcess, Popen, TimeoutExpired, _mswindows
import sys
import time
from typing import Any, Optional, Sequence, Union

logger = logging.getLogger(__name__)


def run_(
    *popenargs: Union[str, Sequence[str]],
    input: Optional[str] = None,  # NOQA: A002
    capture_output: bool = True,
    timeout: Optional[float] = None,
    check: bool = False,
    text: bool = False,
    start_new_session: bool = True,
    **kwargs: Any,
) -> CompletedProcess:
    """Execute a command in a new process with enhanced process management and cleanup.

    This function extends subprocess.run to properly handle child processes and ensure
    clean termination of process trees, particularly useful for browser automation.

    Ref: https://github.com/ArchiveBox/ArchiveBox/blob/v0.8.6rc0/archivebox/misc/system.py
    Ref: https://github.com/python/cpython/blob/main/Lib/subprocess.py
    """
    pgid = None

    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = PIPE

    if capture_output:
        if ("stdout" in kwargs) or ("stderr" in kwargs):
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE

    try:
        with Popen(*popenargs, start_new_session=start_new_session, text=text, **kwargs) as process:  # NOQA: S603
            try:
                pgid = os.getpgid(process.pid)
                stdout, stderr = process.communicate(input, timeout=timeout)
                retcode = process.poll() or 1  # default to error state

                time.sleep(0.5)  # Add a small delay to ensure Chrome finishes writing

                # Ensure process termination
                if process.poll() is None:  # If process hasn't ended yet
                    _terminate_process(process)

            except TimeoutExpired as e:
                process.kill()
                if _mswindows:
                    # Windows accumulates the output in a single blocking
                    # read() call run on child threads, with the timeout
                    # being done in a join() on those threads.  communicate()
                    # _after_ kill() is required to collect that and add it
                    # to the exception.
                    e.stdout, e.stderr = process.communicate()
                else:
                    # POSIX _communicate already populated the output so
                    # far into the TimeoutExpired exception.
                    process.wait()
                raise

            except Exception:
                process.kill()
                raise

            if check and retcode:
                raise CalledProcessError(  # NOQA: TRY301
                    retcode,
                    process.args,
                    output=stdout,
                    stderr=stderr,
                )

    except Exception:
        logger.exception(f"Failed to execute command: {popenargs[0] if popenargs else ''}")
        raise

    finally:
        # Kill only the process group we created
        if pgid:
            _cleanup_process_group(pgid)

    return CompletedProcess(
        process.args if process else popenargs[0],
        retcode,
        stdout,
        stderr,
    )


def _terminate_process(process: Popen, force: bool = False) -> None:
    """Terminate a process, optionally forcing termination."""
    try:
        if force:
            process.kill()
        else:
            process.terminate()
            try:
                process.wait(timeout=2)
            except TimeoutExpired:
                process.kill()
    except Exception as e:
        logger.debug(f"Error terminating process: {e}")


def _cleanup_process_group(pgid: int) -> None:
    """Clean up an entire process group."""
    try:
        # Try graceful termination first
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(0.5)

        # Force kill if processes remain
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Group already terminated
    except Exception as e:
        logger.debug(f"Failed to clean up process group {pgid}: {e}")
