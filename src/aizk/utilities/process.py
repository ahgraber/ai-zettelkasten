# ruff: NOQA: E731
from collections import deque
import contextlib
import datetime
from functools import wraps
import json
import logging
import os
from pathlib import Path
import signal
from subprocess import PIPE, CalledProcessError, CompletedProcess, Popen, TimeoutExpired, _mswindows
import sys
import time
from typing import Any, Optional, Sequence, Union
from uuid import UUID, uuid4

import psutil

import openai

from aizk.utilities import path_is_dir, path_is_file

logger = logging.getLogger(__name__)


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


class BatchHandler:
    """Create and manage batch processing of OpenAI API calls.

    Example (chat, with context manager):

    ```python
    import openai
    from assistant.utilities.process import BatchHandler

    client = openai.Client()
    model = "gpt-4o-mini"
    endpoint = "/v1/chat/completions"
    batch_dir = Path("batches")
    filename_prefix = "batch"

    with BatchHandler(client, model, endpoint, batch_dir, filename_prefix) as bh:
        messages = [{"role": "system", "content": "What is the capital of the United States?"}]
        bh.make_chat_batch(messages)
        bh.upload_batch()
        bh.submit_batch()
        bh.wait_for_batch_completion()  # this will tie up the process until all batches are completed
        bh.save_batch_results()
    ```

    Example (embeddings, without context manager):

    ```python
    ...
    bh = BatchHandler(client, model, endpoint, batch_dir, filename_prefix)
    embeddings = ["this text will be vectorized"]
    bh.make_embeddings_batch(embeddings)
    bh.upload_batch()
    bh.submit_batch()

    # this allows you to come back later and check the status of the batch
    if bh.check_batch_completion():
        bh.save_batch_results()

    # cleanup is important!  BatchHandler assumes
    bh.cleanup()
    ```
    """

    max_batch_records: int = 50_000
    max_batch_size: int = 200  # MB
    bytes_to_size: int = 1024 * 1024
    endpoint_paths: set[str] = {"/v1/chat/completions", "/v1/embeddings"}

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        endpoint: str = "/v1/chat/completions",
        batch_dir: Path = Path("./.batches"),  # this will create a
        filename_prefix: str = "batch",
        mkdir: bool = False,
    ):
        self.client = client
        self.model = model

        if endpoint in self.endpoint_paths:
            self.endpoint = endpoint
        else:
            raise ValueError(f"Endpoint must be in {self.endpoint_paths}. Received: {endpoint}")

        if mkdir:
            Path(batch_dir).mkdir(exist_ok=True, parents=True)
        if Path(batch_dir).is_dir():
            self.batch_dir = Path(batch_dir)
        else:
            raise NotADirectoryError(f"Path is not a directory: {batch_dir}")

        self.filename_prefix = filename_prefix
        self.batch_infos: list = []
        self.file_ids: list = []

    # use as context manager
    def __enter__(self):  # NOQA: D105
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # NOQA: D105
        self.cleanup()

    def _batch_api_calls(self, jsonl: list[dict[str, Any]]):
        """Create batches of api calls."""
        if (not jsonl) or (len(jsonl) == 0):
            raise ValueError("No items provided / 'jsonl' is empty.")

        batch = []
        batch_size = 0
        for item in jsonl:
            item_size = len(json.dumps(item).encode("utf-8")) / self.bytes_to_size

            if (len(batch) >= self.max_batch_records) or (batch_size + item_size >= self.max_batch_size):
                yield batch
                batch = []
                batch_size = 0

            batch.append(item)
            batch_size += item_size

        if batch:
            yield batch

    def _save_batch_file(self, api_calls: list[dict[str, str]]):
        """Save batch to file."""
        for batch in self._batch_api_calls(api_calls):
            fname = f"{self.filename_prefix}_{str(uuid4())}"
            fpath = self.batch_dir / f"{fname}.jsonl"
            if fpath.exists():
                raise FileExistsError(f"{str(fpath)} exists. This should not occur!")

            with fpath.open("w") as f:
                for item in batch:
                    f.write(json.dumps(item) + "\n")

            self.file_ids.append(fname)

    def make_embeddings_batch(
        self,
        inputs: list[str],
        *,
        custom_ids: list[str | int] | None = None,
        embeddings_kwargs: dict[str, Any] | None = None,
    ):
        """Create batch file(s) for processing."""
        if "embeddings" not in self.endpoint:
            raise ValueError("BatchHandler is configured for chat completions, not embeddings.")

        embeddings_kwargs = embeddings_kwargs or {}

        if custom_ids is None:
            custom_ids = list(range(inputs))
        else:
            if len(inputs) != len(custom_ids):
                raise ValueError("'inputs' and 'custom_ids' must have same length!")

        api_calls = [
            {
                "custom_id": str(id_),
                "method": "POST",
                "url": self.endpoint,
                "body": {
                    "model": self.model,
                    "input": input_,
                    **embeddings_kwargs,
                },
            }
            for id_, input_ in zip(custom_ids, inputs)
        ]
        self._save_batch_file(api_calls)

    def make_chat_batch(
        self,
        conversations: list[list[dict[str, str]]],
        *,
        custom_ids: list[str | int] | None = None,
        completions_kwargs: dict[str, Any] | None = None,
    ):
        """Create batch file(s) for processing."""
        if "embeddings" in self.endpoint:
            raise ValueError("BatchHandler is configured for embeddings, not chat completions.")

        completions_kwargs = completions_kwargs or {}

        if custom_ids is None:
            custom_ids = list(range(conversations))
        else:
            if len(conversations) != len(custom_ids):
                raise ValueError("'conversations' and 'custom_ids' must have same length!")

        api_calls = [
            {
                "custom_id": str(id_),
                "method": "POST",
                "url": self.endpoint,
                "body": {
                    "model": self.model,
                    "messages": messages,
                    **completions_kwargs,
                },
            }
            for id_, messages in zip(custom_ids, conversations)
        ]
        self._save_batch_file(api_calls)

    def upload_batch(self, files: list[str | Path] | None = None):
        """Upload batchfiles."""
        if self.batch_infos:
            raise AttributeError("'batch_infos' is not None; has this object already been used?")

        # if files not provided, collect full paths from file_ids tracked in handler
        if not files:
            files = [
                file
                for file in self.batch_dir.glob(f"{self.filename_prefix}_*.jsonl")
                if any(fid in str(file) for fid in self.file_ids)
            ]

        for file in files:
            file = Path(file)
            queued = self.client.files.create(file=file.open("rb"), purpose="batch")
            self.batch_infos.append({"batchfile": file.name, "file_id": queued.id})

        logger.info("Waiting 60 seconds for initial processing...")
        time.sleep(60)

    def submit_batch(self):
        """Submit batches for processing."""
        try:
            for info in self.batch_infos:
                batch_response = self.client.batches.create(
                    input_file_id=info["file_id"],
                    endpoint=self.endpoint,
                    completion_window="24h",  # If you set any other value than 24h your job will fail.
                    # Jobs may take less than 24 hours;
                    # Jobs taking longer than 24 hours will continue to execute until cancelled.
                )

                # Save batch ID for later use
                info["batch_id"] = batch_response.id
                info["status"] = "validating"

            logger.info(json.dumps(self.batch_infos, indent=2))
        except openai.BadRequestError:
            logger.exception("File(s) still processing.  Wait a bit and try again.")

    def _check_status(self, info: dict[str, Any]):
        """Check status of batch job."""
        required_keys = ["batch_id", "batchfile", "file_id"]
        if not all(required in info for required in required_keys):
            raise ValueError(f"batch_info missing a required key in {required_keys}")

        batch_response = self.client.batches.retrieve(info["batch_id"])
        status = batch_response.status
        logger.info(
            f"{datetime.datetime.now()} batchfile: {info['batchfile']} Batch Id: {info['batch_id']},  Status: {status}"
        )
        if status == "failed":
            for error in batch_response.errors.data:
                logger.error(f"Error code {error.code} Message {error.message}")

        return status

    def check_batch_completion(self) -> bool | None:
        """Check the status of all batch jobs."""
        statuses = {self._check_status(info) for info in self.batch_infos}
        if statuses == {"completed"}:
            logger.info("Batch process(es) completed successfully!")
            return True
        elif statuses == {"failed"}:
            logger.error("All batch process(es) failed!")
            return False
        elif statuses == {"cancelled"}:
            logger.info("All batch process(es) cancelled!")
            return False
        return None

    def wait_for_batch_completion(self, sleep: int = 60):
        """Monitor running batch jobs."""
        # for info in self.batch_infos:
        #     info["status"] = "validating"

        while True:
            # Check the overall status of batches
            result = self.check_batch_completion()
            if result is not None:
                return result

            # Wait for the next cycle
            time.sleep(sleep)

    def save_batch_results(self):
        """Retrieve / download processed data."""
        result_infos = []
        for info in self.batch_infos:
            result = self.client.batches.retrieve(info["batch_id"]).model_dump()
            result["filename"] = info["batchfile"]
            result_infos.append(result)

        for info in result_infos:
            output_file_id = info["output_file_id"]
            if not output_file_id:
                output_file_id = info["error_file_id"]

            if output_file_id:
                file_response = self.client.files.content(output_file_id)
                with (self.batch_dir / f"processed_{info['filename']}").open("w") as f:
                    f.write(file_response.text)

    def cleanup(
        self,
        # include_outfiles: bool = False,
    ):
        """Clean up batch files."""
        for info in self.batch_infos:
            try:
                self.client.files.delete(info["file_id"])
            except Exception:
                logger.warning(f"Failed to delete file {info['file_id']}")
                pass

            try:
                (self.batch_dir / info["batchfile"]).unlink()
            except Exception:
                logger.warning(f"Failed to delete file {info['file_id']}")
                pass

            # if include_outfiles:
            #     try:
            #         (self.batch_dir / f"processed_{info['batchfile']}").unlink()
            #     except Exception:
            #         logger.warning(f"Failed to delete file processed_{info['file_id']}")
            #         pass

        self.batch_infos = []
        self.file_ids = []


# %%
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
