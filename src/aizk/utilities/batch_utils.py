import datetime
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Literal, cast
from uuid import uuid4

from tqdm.auto import tqdm

import openai

logger = logging.getLogger(__name__)


class BatchError(Exception):
    """Base exception for batch processing errors."""


class BatchValidationError(BatchError):
    """Exception for batch validation errors."""


class BatchProcessingError(BatchError):
    """Exception for batch processing errors."""


class BatchHandler:
    """Batch processing handler for OpenAI API calls.

    This class provides a streamlined interface for creating, managing, and processing
    OpenAI batch requests with error handling, retry logic, and usability.

    Features:
    - Automatic file management and cleanup
    - Progress tracking with tqdm
    - Retry logic for transient failures
    - Better error reporting
    - Type safety with proper annotations
    - Flexible configuration options

    Example (chat completions):
    ```python
    import openai
    from aizk.utilities.batch_handler import BatchHandler

    client = openai.Client()

    # Simple usage
    with BatchHandler(client, "gpt-4o-mini") as handler:
        conversations = [
            [{"role": "user", "content": "What is AI?"}],
            [{"role": "user", "content": "Explain machine learning"}],
        ]
        results = handler.process_chat_batch(conversations)
        print(results)
    ```

    Example (embeddings):
    ```python
    with BatchHandler(client, "text-embedding-3-small", endpoint="/v1/embeddings") as handler:
        texts = ["Hello world", "How are you?"]
        results = handler.process_embeddings_batch(texts)
        print(results)
    ```
    """

    # OpenAI batch API limits
    MAX_BATCH_RECORDS: int = 50_000
    MAX_BATCH_SIZE_MB: int = 200
    BYTES_TO_MB: float = 1024 * 1024

    # Supported endpoints
    SUPPORTED_ENDPOINTS: set[str] = {"/v1/chat/completions", "/v1/embeddings"}

    # Default retry configuration
    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_RETRY_DELAY: float = 1.0
    DEFAULT_POLL_INTERVAL: int = 60

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        endpoint: str = "/v1/chat/completions",
        batch_dir: Path | str = Path("./.batches"),
        filename_prefix: str = "batch",
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        auto_cleanup: bool = True,
        show_progress: bool = True,
    ):
        """Initialize the BatchHandler.

        Args:
            client: OpenAI client instance
            model: Model name to use for requests
            endpoint: API endpoint ("/v1/chat/completions" or "/v1/embeddings")
            batch_dir: Directory to store batch files
            filename_prefix: Prefix for batch filenames
            max_retries: Maximum number of retries for failed operations
            retry_delay: Base delay between retries (exponential backoff)
            poll_interval: Interval in seconds between status checks
            auto_cleanup: Whether to automatically clean up files
            show_progress: Whether to show progress bars

        Raises:
            ValueError: If endpoint is not supported
            NotADirectoryError: If batch_dir is not a valid directory
        """
        self.client = client
        self.model = model
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.poll_interval = poll_interval
        self.auto_cleanup = auto_cleanup
        self.show_progress = show_progress

        # Validate endpoint
        if endpoint not in self.SUPPORTED_ENDPOINTS:
            raise ValueError(f"Endpoint must be one of {self.SUPPORTED_ENDPOINTS}. Received: {endpoint}")
        self.endpoint = endpoint

        # Setup batch directory
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(exist_ok=True, parents=True)
        if not batch_dir.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {batch_dir}")
        self.batch_dir = batch_dir

        self.filename_prefix = filename_prefix
        self._batch_info: dict[str, Any] = {}
        self._batch_files: list[Path] = []

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        if self.auto_cleanup:
            self.cleanup()

    def _retry_operation(self, operation, *args, **kwargs):
        """Execute operation with retry logic."""
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                return operation(*args, **kwargs)
            except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as e:
                last_exception = e
                if attempt == self.max_retries:
                    break

                delay = self.retry_delay * (2**attempt)
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f} seconds...")
                time.sleep(delay)
            except Exception as e:
                # Non-retryable errors
                raise BatchProcessingError(f"Operation failed: {e}") from e

        raise BatchProcessingError(
            f"Operation failed after {self.max_retries + 1} attempts: {last_exception}"
        ) from last_exception

    def _create_batch_chunks(self, requests: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Split requests into chunks that respect batch limits."""
        if not requests:
            raise BatchValidationError("No requests provided")

        chunks = []
        current_chunk = []
        current_size_mb = 0.0

        for request in requests:
            # Calculate request size
            request_size_mb = len(json.dumps(request).encode("utf-8")) / self.BYTES_TO_MB

            # Check if we need to start a new chunk
            should_start_new_chunk = (
                len(current_chunk) >= self.MAX_BATCH_RECORDS
                or current_size_mb + request_size_mb > self.MAX_BATCH_SIZE_MB
            )

            if should_start_new_chunk and current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_size_mb = 0.0

            current_chunk.append(request)
            current_size_mb += request_size_mb

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _save_batch_file(self, requests: list[dict[str, Any]]) -> Path:
        """Save batch requests to a JSONL file."""
        filename = f"{self.filename_prefix}_{uuid4()}.jsonl"
        filepath = self.batch_dir / filename

        if filepath.exists():
            raise FileExistsError(f"File already exists: {filepath}")

        with filepath.open("w", encoding="utf-8") as f:
            for request in requests:
                f.write(json.dumps(request) + "\n")

        self._batch_files.append(filepath)
        return filepath

    def _create_api_requests(
        self,
        data: list[Any],
        request_builder: Callable,
        custom_ids: list[str | int] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Create API request objects for batch processing."""
        if custom_ids is None:
            custom_ids = [str(i) for i in range(len(data))]
        elif len(data) != len(custom_ids):
            raise BatchValidationError(f"Data length ({len(data)}) must match custom_ids length ({len(custom_ids)})")

        return [request_builder(item, str(custom_id), **kwargs) for item, custom_id in zip(data, custom_ids)]

    def _build_chat_request(
        self,
        messages: list[dict[str, str]],
        custom_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a chat completion request."""
        return {
            "custom_id": custom_id,
            "method": "POST",
            "url": self.endpoint,
            "body": {
                "model": self.model,
                "messages": messages,
                **kwargs,
            },
        }

    def _build_embeddings_request(
        self,
        text: str,
        custom_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build an embeddings request."""
        return {
            "custom_id": custom_id,
            "method": "POST",
            "url": self.endpoint,
            "body": {
                "model": self.model,
                "input": text,
                **kwargs,
            },
        }

    def _validate_file_status(self, file_id: str) -> str:
        """Validate file processing status.

        Args:
            file_id: The file ID to check

        Returns:
            The current status of the file

        Raises:
            BatchProcessingError: If file processing failed or status check failed
        """
        try:
            file_obj = self.client.files.retrieve(file_id)
            status = getattr(file_obj, "status", "unknown")
        except BatchProcessingError:
            raise
        except Exception as e:
            raise BatchProcessingError(f"Error checking file status: {e}") from e

        if status == "error":
            logger.error(f"File {file_id} processing failed")
            raise BatchProcessingError(f"File {file_id} processing failed")

        return status

    def _wait_for_file_processing(self, file_id: str, timeout: int = 300) -> None:
        """Wait for file to be processed by OpenAI.

        Args:
            file_id: The file ID to wait for
            timeout: Maximum time to wait in seconds (default: 5 minutes)

        Raises:
            BatchProcessingError: If file processing fails or times out
        """
        start_time = time.time()
        pbar = None

        if self.show_progress:
            pbar = tqdm(desc="Waiting for file processing", unit="check", leave=False)

        try:
            while time.time() - start_time < timeout:
                status = self._validate_file_status(file_id)

                if status == "processed":
                    logger.info(f"File {file_id} processed successfully")
                    return

                # File still processing, wait a bit
                if pbar:
                    elapsed = time.time() - start_time
                    pbar.set_postfix(status=status, elapsed=f"{elapsed:.1f}s")

                # Use a shorter sleep interval for more responsive checking
                sleep_interval = min(2.0, max(0.1, timeout / 10))
                time.sleep(sleep_interval)

            # Timeout reached
            raise BatchProcessingError(f"File {file_id} processing timed out after {timeout} seconds")

        finally:
            if pbar:
                pbar.close()

    def _upload_and_submit_batch(self, filepath: Path) -> str:
        """Upload file and submit batch job."""

        # Upload file
        def upload_file():
            with filepath.open("rb") as f:
                return self.client.files.create(file=f, purpose="batch")

        file_obj = self._retry_operation(upload_file)
        file_id = file_obj.id

        # Wait for file processing with proper polling
        self._wait_for_file_processing(file_id)

        # Submit batch
        def submit_batch():
            # Type cast to satisfy OpenAI client typing
            endpoint_literal = cast(Literal["/v1/chat/completions", "/v1/embeddings"], self.endpoint)
            return self.client.batches.create(
                input_file_id=file_id,
                endpoint=endpoint_literal,
                completion_window="24h",
            )

        batch_obj = self._retry_operation(submit_batch)
        batch_id = batch_obj.id

        # Store batch info
        self._batch_info[batch_id] = {
            "file_id": file_id,
            "filepath": filepath,
            "status": "submitted",
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }

        logger.info(f"Submitted batch {batch_id}")
        return batch_id

    def _wait_for_completion(self, batch_ids: list[str]) -> dict[str, Any]:
        """Wait for batch completion and return results."""
        pbar = None
        if self.show_progress:
            pbar = tqdm(
                desc="Waiting for batch completion",
                unit="check",
                leave=False,
            )

        completed_batches = set()
        total_batches = len(batch_ids)

        try:
            while len(completed_batches) < total_batches:
                for batch_id in batch_ids:
                    if batch_id in completed_batches:
                        continue

                    try:
                        batch_obj = self.client.batches.retrieve(batch_id)
                        status = batch_obj.status
                        self._batch_info[batch_id]["status"] = status

                        if status == "completed":
                            completed_batches.add(batch_id)
                            logger.info(f"Batch {batch_id} completed successfully")
                        elif status == "failed":
                            completed_batches.add(batch_id)
                            error_info = getattr(batch_obj, "errors", None)
                            if error_info and hasattr(error_info, "data"):
                                for error in error_info.data:
                                    logger.error(
                                        f"Batch {batch_id} failed - Code: {error.code}, Message: {error.message}"
                                    )
                            else:
                                logger.error(f"Batch {batch_id} failed - No error details available")
                        elif status == "cancelled":
                            completed_batches.add(batch_id)
                            logger.warning(f"Batch {batch_id} was cancelled")

                    except Exception:
                        logger.exception(f"Error checking batch {batch_id}")

                if pbar:
                    pbar.set_postfix(
                        completed=len(completed_batches),
                        total=total_batches,
                        refresh=True,
                    )

                if len(completed_batches) < total_batches:
                    time.sleep(self.poll_interval)

        finally:
            if pbar:
                pbar.close()

        return self._batch_info

    def _download_results(self, batch_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Download and parse batch results."""
        all_results = {}

        for batch_id in batch_ids:
            batch_info = self._batch_info[batch_id]
            if batch_info["status"] != "completed":
                logger.warning(f"Skipping batch {batch_id} (status: {batch_info['status']})")
                continue

            try:
                batch_obj = self.client.batches.retrieve(batch_id)
                if not batch_obj.output_file_id:
                    logger.warning(f"No output file for batch {batch_id}")
                    continue

                # Download results
                file_content = self.client.files.content(batch_obj.output_file_id)

                # Parse JSONL results and organize by custom_id
                for line in file_content.text.strip().split("\n"):
                    if line:
                        result = json.loads(line)
                        custom_id = result["custom_id"]
                        if custom_id not in all_results:
                            all_results[custom_id] = []
                        all_results[custom_id].append(result)

                # Clean up remote file
                if self.auto_cleanup:
                    try:
                        self.client.files.delete(batch_obj.output_file_id)
                        self.client.files.delete(batch_info["file_id"])
                    except Exception as e:
                        logger.warning(f"Failed to delete remote files for batch {batch_id}: {e}")

            except Exception:
                logger.exception(f"Failed to download results for batch {batch_id}")

        return all_results

    def process_chat_batch(
        self,
        conversations: list[list[dict[str, str]]],
        custom_ids: list[str | int] | None = None,
        **completions_kwargs: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        """Process a batch of chat completion requests.

        Args:
            conversations: List of conversation message lists
            custom_ids: Optional custom identifiers for each conversation
            **completions_kwargs: Additional parameters for chat completions

        Returns:
            Dictionary mapping batch IDs to lists of results

        Raises:
            BatchValidationError: If input validation fails
            BatchProcessingError: If processing fails
        """
        if self.endpoint != "/v1/chat/completions":
            raise BatchValidationError("Handler is configured for embeddings, not chat completions")

        # Create API requests
        requests = self._create_api_requests(
            conversations,
            self._build_chat_request,
            custom_ids,
            **completions_kwargs,
        )

        # Process in chunks
        chunks = self._create_batch_chunks(requests)
        batch_ids = []

        logger.info(f"Processing {len(requests)} requests in {len(chunks)} batch(es)")

        for i, chunk in enumerate(chunks):
            logger.info(f"Processing chunk {i + 1}/{len(chunks)} ({len(chunk)} requests)")
            filepath = self._save_batch_file(chunk)
            batch_id = self._upload_and_submit_batch(filepath)
            batch_ids.append(batch_id)

        # Wait for completion and download results
        self._wait_for_completion(batch_ids)
        return self._download_results(batch_ids)

    def process_embeddings_batch(
        self,
        texts: list[str],
        custom_ids: list[str | int] | None = None,
        **embeddings_kwargs: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        """Process a batch of embedding requests.

        Args:
            texts: List of texts to embed
            custom_ids: Optional custom identifiers for each text
            **embeddings_kwargs: Additional parameters for embeddings

        Returns:
            Dictionary mapping batch IDs to lists of results

        Raises:
            BatchValidationError: If input validation fails
            BatchProcessingError: If processing fails
        """
        if self.endpoint != "/v1/embeddings":
            raise BatchValidationError("Handler is configured for chat completions, not embeddings")

        # Create API requests
        requests = self._create_api_requests(
            texts,
            self._build_embeddings_request,
            custom_ids,
            **embeddings_kwargs,
        )

        # Process in chunks
        chunks = self._create_batch_chunks(requests)
        batch_ids = []

        logger.info(f"Processing {len(requests)} requests in {len(chunks)} batch(es)")

        for i, chunk in enumerate(chunks):
            logger.info(f"Processing chunk {i + 1}/{len(chunks)} ({len(chunk)} requests)")
            filepath = self._save_batch_file(chunk)
            batch_id = self._upload_and_submit_batch(filepath)
            batch_ids.append(batch_id)

        # Wait for completion and download results
        self._wait_for_completion(batch_ids)
        return self._download_results(batch_ids)

    def cleanup(self):
        """Clean up local files and resources."""
        logger.info("Cleaning up batch files...")

        for filepath in self._batch_files:
            try:
                if filepath.exists():
                    filepath.unlink()
                    logger.debug(f"Deleted local file: {filepath}")
            except Exception as e:
                logger.warning(f"Failed to delete local file {filepath}: {e}")

        self._batch_files.clear()
        self._batch_info.clear()

    def get_batch_status(self, batch_id: str) -> dict[str, Any]:
        """Get the status of a specific batch.

        Args:
            batch_id: The batch ID to check

        Returns:
            Dictionary with batch status information
        """
        try:
            batch_obj = self.client.batches.retrieve(batch_id)
            return {
                "id": batch_obj.id,
                "status": batch_obj.status,
                "created_at": batch_obj.created_at,
                "completed_at": getattr(batch_obj, "completed_at", None),
                "failed_at": getattr(batch_obj, "failed_at", None),
                "request_counts": getattr(batch_obj, "request_counts", {}),
            }
        except Exception as e:
            raise BatchProcessingError(f"Failed to get batch status: {e}") from e

    def cancel_batch(self, batch_id: str) -> bool:
        """Cancel a running batch.

        Args:
            batch_id: The batch ID to cancel

        Returns:
            True if cancellation was successful, False otherwise
        """
        try:
            self.client.batches.cancel(batch_id)
            logger.info(f"Cancelled batch {batch_id}")
        except Exception:
            logger.exception(f"Failed to cancel batch {batch_id}")
            return False
        else:
            return True
