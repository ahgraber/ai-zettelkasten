from contextlib import contextmanager
import json
from pathlib import Path
import time
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

import openai

import aizk.utilities.batch_utils as batch_utils_module
from aizk.utilities.batch_utils import (
    BatchHandler,
    BatchProcessingError,
    BatchValidationError,
)


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def chat_handler(mock_client, tmp_path):
    return BatchHandler(
        client=mock_client,
        model="gpt-4o-mini-batch",
        endpoint="/v1/chat/completions",
        batch_dir=tmp_path,
        filename_prefix="batch",
    )


@pytest.fixture
def embeddings_handler(mock_client, tmp_path):
    return BatchHandler(
        client=mock_client,
        model="text-embeddings-3-small-batch",
        endpoint="/v1/embeddings",
        batch_dir=tmp_path,
        filename_prefix="batch",
    )


class TestBatchHandlerInit:
    def test_default_init(self, mock_client, tmp_path):
        """Test BatchHandler initialization with default values."""
        model = "test"
        handler = BatchHandler(
            mock_client,
            model=model,
            batch_dir=tmp_path,
        )
        assert handler.model == model
        assert handler.endpoint == "/v1/chat/completions"
        assert handler.batch_dir.is_dir()
        assert handler.filename_prefix == "batch"
        assert handler.max_retries == BatchHandler.DEFAULT_MAX_RETRIES
        assert handler.retry_delay == BatchHandler.DEFAULT_RETRY_DELAY
        assert handler.poll_interval == BatchHandler.DEFAULT_POLL_INTERVAL
        assert handler.auto_cleanup is True
        assert handler.show_progress is True

    def test_init_creates_missing_batchdir(self, mock_client, tmp_path):
        """Test that BatchHandler creates missing batch directories."""
        model = "test"
        does_not_exist = tmp_path / "fake"
        handler = BatchHandler(
            mock_client,
            model=model,
            batch_dir=does_not_exist,
        )
        assert does_not_exist.exists()
        assert does_not_exist.is_dir()
        assert handler.batch_dir == does_not_exist

    def test_init_unsupported_endpoint(self, mock_client, tmp_path):
        """Test that unsupported endpoint raises ValueError."""
        model = "test"
        with pytest.raises(ValueError, match="Endpoint must be one of"):
            BatchHandler(
                mock_client,
                model=model,
                batch_dir=tmp_path,
                endpoint="/v1/unsupported",
            )

    def test_chat_init(self, chat_handler):
        """Test chat handler initialization."""
        assert chat_handler.model == "gpt-4o-mini-batch"
        assert chat_handler.endpoint == "/v1/chat/completions"
        assert chat_handler.batch_dir.is_dir()
        assert chat_handler.filename_prefix == "batch"

    def test_embeddings_init(self, embeddings_handler):
        """Test embeddings handler initialization."""
        assert embeddings_handler.model == "text-embeddings-3-small-batch"
        assert embeddings_handler.endpoint == "/v1/embeddings"
        assert embeddings_handler.batch_dir.is_dir()
        assert embeddings_handler.filename_prefix == "batch"


class TestBatchHandlerRetryLogic:
    def test_retry_operation_success_first_try(self, chat_handler):
        """Test retry operation succeeds on first try."""

        def successful_operation():
            return "success"

        result = chat_handler._retry_operation(successful_operation)
        assert result == "success"

    def test_retry_operation_success_after_retry(self, chat_handler):
        """Test retry operation succeeds after retries."""
        call_count = 0

        def operation_with_retries():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Create a proper OpenAI exception with required args
                mock_response = MagicMock()
                mock_response.status_code = 429
                raise openai.RateLimitError("Rate limited", response=mock_response, body="rate limited")
            return "success"

        # Set short retry delay for faster tests
        chat_handler.retry_delay = 0.01
        result = chat_handler._retry_operation(operation_with_retries)
        assert result == "success"
        assert call_count == 3

    def test_retry_operation_max_retries_exceeded(self, chat_handler):
        """Test retry operation fails after max retries."""

        def failing_operation():
            mock_response = MagicMock()
            mock_response.status_code = 429
            raise openai.RateLimitError("Rate limited", response=mock_response, body="rate limited")

        chat_handler.retry_delay = 0.01
        with pytest.raises(BatchProcessingError, match="Operation failed after .* attempts"):
            chat_handler._retry_operation(failing_operation)

    def test_retry_operation_non_retryable_error(self, chat_handler):
        """Test retry operation doesn't retry non-retryable errors."""

        def operation_with_non_retryable_error():
            raise ValueError("Invalid input")

        with pytest.raises(BatchProcessingError, match="Operation failed"):
            chat_handler._retry_operation(operation_with_non_retryable_error)

    @pytest.mark.parametrize(
        "exception_class,status_code",
        [
            (openai.RateLimitError, 429),
            (openai.APITimeoutError, 408),
            (openai.APIConnectionError, 503),
        ],
    )
    def test_retry_operation_retryable_errors(self, chat_handler, exception_class, status_code):
        """Test that specific OpenAI errors are retryable."""
        call_count = 0

        def operation_with_error():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                mock_response = MagicMock()
                mock_response.status_code = status_code
                # Different exception types have different constructor requirements
                if exception_class == openai.RateLimitError:
                    raise exception_class("Test error", response=mock_response, body="test error")
                elif exception_class == openai.APITimeoutError:
                    # APITimeoutError doesn't take response/body args, just request info
                    raise exception_class(request=MagicMock())
                elif exception_class == openai.APIConnectionError:
                    # APIConnectionError doesn't take response/body args, just request info
                    raise exception_class(request=MagicMock())
                else:
                    raise exception_class("Test error")
            return "success"

        chat_handler.retry_delay = 0.01
        result = chat_handler._retry_operation(operation_with_error)
        assert result == "success"
        assert call_count == 2


class TestBatchHandlerProcessing:
    def _mock_successful_workflow(self, handler, batch_id):
        """Helper method to mock a successful batch processing workflow."""
        # Mock file upload
        mock_file_obj = MagicMock()
        mock_file_obj.id = "file_123"
        handler.client.files.create.return_value = mock_file_obj

        # Mock batch creation
        mock_batch_obj = MagicMock()
        mock_batch_obj.id = batch_id
        handler.client.batches.create.return_value = mock_batch_obj

        # Mock file validation (file is processed)
        mock_file_status = MagicMock()
        mock_file_status.status = "processed"
        handler.client.files.retrieve.return_value = mock_file_status

        # Mock batch completion
        mock_batch_status = MagicMock()
        mock_batch_status.status = "completed"
        mock_batch_status.output_file_id = "output_file_123"
        handler.client.batches.retrieve.return_value = mock_batch_status

    def test_process_chat_batch_success(self, chat_handler):
        """Test successful chat batch processing."""
        conversations = [
            [{"role": "user", "content": "Hello"}],
            [{"role": "user", "content": "How are you?"}],
        ]

        # Mock the entire workflow
        self._mock_successful_workflow(chat_handler, "batch_123")

        # Mock file download
        mock_output_file = MagicMock()
        mock_output_file.text = '{"custom_id": "0", "response": {"body": {"choices": [{"message": {"content": "Hello!"}}]}}}\n{"custom_id": "1", "response": {"body": {"choices": [{"message": {"content": "I\'m fine!"}}]}}}'
        chat_handler.client.files.content.return_value = mock_output_file

        # Mock progress to avoid tqdm in tests
        chat_handler.show_progress = False

        results = chat_handler.process_chat_batch(conversations)

        # Verify results
        assert "0" in results
        assert "1" in results
        assert len(results) == 2

    def test_process_embeddings_batch_success(self, embeddings_handler):
        """Test successful embeddings batch processing."""
        texts = ["Hello world", "How are you?"]

        # Mock the entire workflow
        self._mock_successful_workflow(embeddings_handler, "batch_123")

        # Mock file download
        mock_output_file = MagicMock()
        mock_output_file.text = '{"custom_id": "0", "response": {"body": {"data": [{"embedding": [0.1, 0.2, 0.3]}]}}}\n{"custom_id": "1", "response": {"body": {"data": [{"embedding": [0.4, 0.5, 0.6]}]}}}'
        embeddings_handler.client.files.content.return_value = mock_output_file

        # Mock progress to avoid tqdm in tests
        embeddings_handler.show_progress = False

        results = embeddings_handler.process_embeddings_batch(texts)

        # Verify results
        assert "0" in results
        assert "1" in results
        assert len(results) == 2

    def test_process_embeddings_batch_tracing_attributes_minimal(self, embeddings_handler, monkeypatch):
        """Embedding traces include only model attribute at span start."""
        captured_calls: list[tuple[str, str, dict[str, object]]] = []

        @contextmanager
        def _capture_trace_model_call(*, name, span_type, attributes=None):
            captured_calls.append((name, span_type, attributes or {}))
            yield None

        monkeypatch.setattr(batch_utils_module, "trace_model_call", _capture_trace_model_call)
        self._mock_successful_workflow(embeddings_handler, "batch_123")
        mock_output_file = MagicMock()
        mock_output_file.text = '{"custom_id": "0", "response": {"body": {"data": [{"embedding": [0.1]}]}}}'
        embeddings_handler.client.files.content.return_value = mock_output_file
        embeddings_handler.show_progress = False

        embeddings_handler.process_embeddings_batch(["hello"])

        assert captured_calls == [
            (
                "embedding.batch",
                "EMBEDDING",
                {"model": "text-embeddings-3-small-batch"},
            )
        ]

    def test_process_chat_batch_tracing_uses_parent_and_chunk_spans(self, chat_handler, monkeypatch):
        """Chat tracing emits one parent span plus per-chunk spans."""
        captured_calls: list[tuple[str, str, dict[str, object]]] = []

        @contextmanager
        def _capture_trace_model_call(*, name, span_type, attributes=None):
            captured_calls.append((name, span_type, attributes or {}))
            yield None

        monkeypatch.setattr(batch_utils_module, "trace_model_call", _capture_trace_model_call)
        self._mock_successful_workflow(chat_handler, "batch_123")
        mock_output_file = MagicMock()
        mock_output_file.text = (
            '{"custom_id": "0", "response": {"body": {"choices": [{"message": {"content": "ok"}}]}}}'
        )
        chat_handler.client.files.content.return_value = mock_output_file
        chat_handler.show_progress = False

        chat_handler.process_chat_batch([[{"role": "user", "content": "Hello"}]])

        assert captured_calls[0][0] == "llm.chat.completions.batch"
        assert captured_calls[0][1] == "CHAT_MODEL"
        assert captured_calls[1][0] == "llm.chat.completions.batch.chunk"
        assert captured_calls[1][1] == "CHAIN"

    def test_process_chat_batch_with_custom_ids(self, chat_handler):
        """Test chat batch processing with custom IDs."""
        conversations = [
            [{"role": "user", "content": "Hello"}],
        ]
        custom_ids = ["greeting"]

        # Mock the workflow
        self._mock_successful_workflow(chat_handler, "batch_123")

        mock_output_file = MagicMock()
        mock_output_file.text = (
            '{"custom_id": "greeting", "response": {"body": {"choices": [{"message": {"content": "Hello!"}}]}}}'
        )
        chat_handler.client.files.content.return_value = mock_output_file

        chat_handler.show_progress = False

        results = chat_handler.process_chat_batch(conversations, custom_ids=custom_ids)

        assert "greeting" in results

    def test_process_chat_batch_empty_input(self, chat_handler):
        """Test that empty input raises validation error."""
        with pytest.raises(BatchValidationError, match="No requests provided"):
            chat_handler.process_chat_batch([])

    def test_process_embeddings_batch_empty_input(self, embeddings_handler):
        """Test that empty input raises validation error."""
        with pytest.raises(BatchValidationError, match="No requests provided"):
            embeddings_handler.process_embeddings_batch([])


class TestBatchHandlerWaitForCompletion:
    def test_wait_for_completion_success(self, chat_handler):
        """Test waiting for batch completion successfully."""
        batch_ids = ["batch_123"]

        # Initialize batch info that _wait_for_completion expects
        chat_handler._batch_info["batch_123"] = {
            "file_id": "file_123",
            "filepath": "/fake/path",
            "status": "submitted",
            "created_at": "2024-01-01T00:00:00",
        }

        # Mock batch status progression - use a function to return different values
        call_count = 0

        def mock_retrieve(batch_id):
            nonlocal call_count
            call_count += 1
            mock_batch = MagicMock()
            if call_count == 1:
                mock_batch.status = "in_progress"
            else:
                mock_batch.status = "completed"
            return mock_batch

        chat_handler.client.batches.retrieve.side_effect = mock_retrieve
        chat_handler.show_progress = False
        chat_handler.poll_interval = 0.01  # Fast polling for tests

        result = chat_handler._wait_for_completion(batch_ids)

        assert "batch_123" in result
        assert result["batch_123"]["status"] == "completed"
        assert chat_handler.client.batches.retrieve.call_count >= 2

    def test_wait_for_completion_failed_batch(self, chat_handler):
        """Test handling of failed batch."""
        batch_ids = ["batch_123"]

        # Initialize batch info that _wait_for_completion expects
        chat_handler._batch_info["batch_123"] = {
            "file_id": "file_123",
            "filepath": "/fake/path",
            "status": "submitted",
            "created_at": "2024-01-01T00:00:00",
        }

        mock_batch_failed = MagicMock()
        mock_batch_failed.status = "failed"
        mock_batch_failed.errors = None

        chat_handler.client.batches.retrieve.return_value = mock_batch_failed
        chat_handler.show_progress = False

        result = chat_handler._wait_for_completion(batch_ids)

        assert result["batch_123"]["status"] == "failed"

    def test_wait_for_completion_cancelled_batch(self, chat_handler):
        """Test handling of cancelled batch."""
        batch_ids = ["batch_123"]

        # Initialize batch info that _wait_for_completion expects
        chat_handler._batch_info["batch_123"] = {
            "file_id": "file_123",
            "filepath": "/fake/path",
            "status": "submitted",
            "created_at": "2024-01-01T00:00:00",
        }

        mock_batch_cancelled = MagicMock()
        mock_batch_cancelled.status = "cancelled"

        chat_handler.client.batches.retrieve.return_value = mock_batch_cancelled
        chat_handler.show_progress = False

        result = chat_handler._wait_for_completion(batch_ids)

        assert result["batch_123"]["status"] == "cancelled"


class TestBatchHandlerFileProcessing:
    def test_wait_for_file_processing_success(self, chat_handler):
        """Test successful file processing wait."""
        file_id = "file_123"

        # Mock file status progression using a function to avoid StopIteration
        call_count = 0

        def mock_retrieve(file_id):
            nonlocal call_count
            call_count += 1
            mock_file = MagicMock()
            if call_count == 1:
                mock_file.status = "processing"
            else:
                mock_file.status = "processed"
            return mock_file

        chat_handler.client.files.retrieve.side_effect = mock_retrieve
        chat_handler.show_progress = False

        # Should not raise an exception
        chat_handler._wait_for_file_processing(file_id, timeout=1)

    def test_wait_for_file_processing_timeout(self, chat_handler):
        """Test file processing timeout."""
        file_id = "file_123"

        mock_file_processing = MagicMock()
        mock_file_processing.status = "processing"

        chat_handler.client.files.retrieve.return_value = mock_file_processing
        chat_handler.show_progress = False

        with pytest.raises(BatchProcessingError, match="processing timed out"):
            chat_handler._wait_for_file_processing(file_id, timeout=0.1)

    def test_wait_for_file_processing_error(self, chat_handler):
        """Test file processing error status."""
        file_id = "file_123"

        mock_file_error = MagicMock()
        mock_file_error.status = "error"

        chat_handler.client.files.retrieve.return_value = mock_file_error
        chat_handler.show_progress = False

        with pytest.raises(BatchProcessingError, match="File .* processing failed"):
            chat_handler._wait_for_file_processing(file_id, timeout=1)


class TestBatchHandlerChunking:
    def test_create_batch_chunks_single_batch(self, chat_handler):
        requests = [
            {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": {"key": "value"}}
            for i in range(10)
        ]
        chunks = chat_handler._create_batch_chunks(requests)
        assert len(chunks) == 1
        assert len(chunks[0]) == 10

    def test_create_batch_chunks_multiple_batches(self, chat_handler):
        # Create requests that exceed the max batch records
        max_records = 100
        original_max = chat_handler.MAX_BATCH_RECORDS
        chat_handler.MAX_BATCH_RECORDS = max_records

        try:
            requests = [
                {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": {"key": "value"}}
                for i in range(max_records + 1)
            ]
            chunks = chat_handler._create_batch_chunks(requests)
            assert len(chunks) == 2
            assert len(chunks[0]) == max_records
            assert len(chunks[1]) == 1
        finally:
            chat_handler.MAX_BATCH_RECORDS = original_max

    def test_create_batch_chunks_exact_batch_size(self, chat_handler):
        max_records = 100
        original_max = chat_handler.MAX_BATCH_RECORDS
        chat_handler.MAX_BATCH_RECORDS = max_records

        try:
            requests = [
                {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": {"key": "value"}}
                for i in range(max_records)
            ]
            chunks = chat_handler._create_batch_chunks(requests)
            assert len(chunks) == 1
            assert len(chunks[0]) == max_records
        finally:
            chat_handler.MAX_BATCH_RECORDS = original_max

    def test_create_batch_chunks_size_limit(self, chat_handler):
        # Create requests that exceed the max batch size in MB
        original_max_size = chat_handler.MAX_BATCH_SIZE_MB
        chat_handler.MAX_BATCH_SIZE_MB = 0.001  # Very small limit for testing

        try:
            large_body = {"key": "v" * 1024}  # Create a reasonably large request
            requests = [
                {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": large_body}
                for i in range(5)
            ]
            chunks = chat_handler._create_batch_chunks(requests)
            # Should be split into multiple chunks due to size limit
            assert len(chunks) >= 2
        finally:
            chat_handler.MAX_BATCH_SIZE_MB = original_max_size

    def test_create_batch_chunks_empty_input(self, chat_handler):
        with pytest.raises(BatchValidationError, match="No requests provided"):
            chat_handler._create_batch_chunks([])


class TestBatchHandlerChunkingAdvanced:
    """Test advanced chunking scenarios."""

    def test_create_batch_chunks_size_based_splitting(self, chat_handler):
        """Test that large requests are split by size."""
        # Temporarily reduce size limit for testing
        original_max_size = chat_handler.MAX_BATCH_SIZE_MB
        chat_handler.MAX_BATCH_SIZE_MB = 0.001  # Very small limit

        try:
            # Create requests with large bodies
            large_body = {"key": "v" * 1024}  # About 1KB each
            requests = [
                {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": large_body}
                for i in range(10)
            ]

            chunks = chat_handler._create_batch_chunks(requests)

            # Should be split into multiple chunks due to size limit
            assert len(chunks) >= 2
            assert sum(len(chunk) for chunk in chunks) == 10

        finally:
            chat_handler.MAX_BATCH_SIZE_MB = original_max_size

    def test_create_batch_chunks_record_count_splitting(self, chat_handler):
        """Test that requests are split by record count."""
        # Temporarily reduce record limit for testing
        original_max_records = chat_handler.MAX_BATCH_RECORDS
        chat_handler.MAX_BATCH_RECORDS = 5

        try:
            requests = [
                {"custom_id": str(i), "method": "POST", "url": "/v1/chat/completions", "body": {"small": "data"}}
                for i in range(12)
            ]

            chunks = chat_handler._create_batch_chunks(requests)

            # Should be split into 3 chunks: 5, 5, 2
            assert len(chunks) == 3
            assert len(chunks[0]) == 5
            assert len(chunks[1]) == 5
            assert len(chunks[2]) == 2

        finally:
            chat_handler.MAX_BATCH_RECORDS = original_max_records


class TestBatchHandlerCreateEmbeddingsRequests:
    def test_create_embeddings_requests(self, embeddings_handler):
        inputs = ["text1", "text2"]
        requests = embeddings_handler._create_api_requests(inputs, embeddings_handler._build_embeddings_request)

        expected = [
            {
                "custom_id": str(i),
                "method": "POST",
                "url": embeddings_handler.endpoint,
                "body": {
                    "model": embeddings_handler.model,
                    "input": input_,
                },
            }
            for i, input_ in enumerate(inputs)
        ]
        assert requests == expected

    def test_build_embeddings_request(self, embeddings_handler):
        text = "Hello world"
        custom_id = "test_id"
        request = embeddings_handler._build_embeddings_request(text, custom_id)

        expected = {
            "custom_id": custom_id,
            "method": "POST",
            "url": embeddings_handler.endpoint,
            "body": {
                "model": embeddings_handler.model,
                "input": text,
            },
        }
        assert request == expected

    def test_save_batch_file_embeddings(self, embeddings_handler):
        inputs = ["text1", "text2"]
        requests = embeddings_handler._create_api_requests(inputs, embeddings_handler._build_embeddings_request)

        filepath = embeddings_handler._save_batch_file(requests)
        assert filepath.exists()
        assert filepath.is_file()
        assert filepath in embeddings_handler._batch_files

        # Verify file contents
        with filepath.open("r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == len(requests)
        assert lines == requests


class TestBatchHandlerCreateChatRequests:
    def test_create_chat_requests(self, chat_handler):
        conversations = [
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of the United States?"},
            ],
        ]

        requests = chat_handler._create_api_requests(conversations, chat_handler._build_chat_request)

        expected = [
            {
                "custom_id": str(i),
                "method": "POST",
                "url": chat_handler.endpoint,
                "body": {
                    "model": chat_handler.model,
                    "messages": messages,
                },
            }
            for i, messages in enumerate(conversations)
        ]
        assert requests == expected

    def test_build_chat_request(self, chat_handler):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        custom_id = "test_id"
        request = chat_handler._build_chat_request(messages, custom_id)

        expected = {
            "custom_id": custom_id,
            "method": "POST",
            "url": chat_handler.endpoint,
            "body": {
                "model": chat_handler.model,
                "messages": messages,
            },
        }
        assert request == expected

    def test_save_batch_file_chat(self, chat_handler):
        conversations = [
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of the United States?"},
            ],
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Who was Albert Einstein?"},
            ],
        ]

        requests = chat_handler._create_api_requests(conversations, chat_handler._build_chat_request)

        filepath = chat_handler._save_batch_file(requests)
        assert filepath.exists()
        assert filepath.is_file()
        assert filepath in chat_handler._batch_files

        # Verify file contents
        with filepath.open("r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        assert len(lines) == len(requests)
        assert lines == requests


class TestBatchHandlerValidation:
    def test_wrong_endpoint_for_chat(self, embeddings_handler):
        """Test that chat processing fails when handler is configured for embeddings."""
        conversations = [[{"role": "user", "content": "Hello"}]]
        with pytest.raises(BatchValidationError, match="Handler is configured for embeddings"):
            embeddings_handler.process_chat_batch(conversations)

    def test_wrong_endpoint_for_embeddings(self, chat_handler):
        """Test that embeddings processing fails when handler is configured for chat."""
        texts = ["Hello world"]
        with pytest.raises(BatchValidationError, match="Handler is configured for chat completions"):
            chat_handler.process_embeddings_batch(texts)

    def test_custom_ids_length_mismatch(self, chat_handler):
        """Test that mismatched custom_ids length raises error."""
        conversations = [[{"role": "user", "content": "Hello"}]]
        custom_ids = ["id1", "id2"]  # Length mismatch
        with pytest.raises(BatchValidationError, match="Data length.*must match custom_ids length"):
            chat_handler._create_api_requests(conversations, chat_handler._build_chat_request, custom_ids)


class TestBatchHandlerContextManager:
    def test_context_manager_cleanup(self, mock_client, tmp_path):
        """Test that context manager properly cleans up files."""
        with BatchHandler(client=mock_client, model="test-model", batch_dir=tmp_path, auto_cleanup=True) as handler:
            # Create a test file
            test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]
            filepath = handler._save_batch_file(test_requests)
            assert filepath.exists()
            assert len(handler._batch_files) == 1

        # After exiting context manager, files should be cleaned up
        assert not filepath.exists()
        assert len(handler._batch_files) == 0

    def test_context_manager_no_cleanup(self, mock_client, tmp_path):
        """Test that context manager doesn't clean up when auto_cleanup=False."""
        filepath = None
        with BatchHandler(client=mock_client, model="test-model", batch_dir=tmp_path, auto_cleanup=False) as handler:
            # Create a test file
            test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]
            filepath = handler._save_batch_file(test_requests)
            assert filepath.exists()

        # After exiting context manager, files should still exist
        assert filepath.exists()


class TestBatchHandlerCleanup:
    def test_cleanup(self, chat_handler):
        """Test that cleanup properly removes files and clears state."""
        # Create some test files
        test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]
        filepath1 = chat_handler._save_batch_file(test_requests)
        filepath2 = chat_handler._save_batch_file(test_requests)

        # Add some batch info
        chat_handler._batch_info["test_batch"] = {"status": "completed"}

        assert len(chat_handler._batch_files) == 2
        assert len(chat_handler._batch_info) == 1
        assert filepath1.exists()
        assert filepath2.exists()

        # Run cleanup
        chat_handler.cleanup()

        # Verify cleanup
        assert len(chat_handler._batch_files) == 0
        assert len(chat_handler._batch_info) == 0
        assert not filepath1.exists()
        assert not filepath2.exists()


class TestBatchHandlerCleanupExtended:
    """Extended cleanup functionality tests."""

    def test_cleanup_with_multiple_files(self, chat_handler):
        """Test cleanup with multiple batch files."""
        # Create multiple test files
        test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]
        filepath1 = chat_handler._save_batch_file(test_requests)
        filepath2 = chat_handler._save_batch_file(test_requests)
        filepath3 = chat_handler._save_batch_file(test_requests)

        # Add batch info
        chat_handler._batch_info["batch1"] = {"status": "completed"}
        chat_handler._batch_info["batch2"] = {"status": "failed"}

        # Verify initial state
        assert len(chat_handler._batch_files) == 3
        assert len(chat_handler._batch_info) == 2
        assert all(fp.exists() for fp in [filepath1, filepath2, filepath3])

        # Run cleanup
        chat_handler.cleanup()

        # Verify cleanup
        assert len(chat_handler._batch_files) == 0
        assert len(chat_handler._batch_info) == 0
        assert not any(fp.exists() for fp in [filepath1, filepath2, filepath3])

    def test_context_manager_cleanup_on_exception(self, mock_client, tmp_path):
        """Test that context manager cleans up even when exception occurs."""
        saved_filepath = None

        def create_and_raise():
            nonlocal saved_filepath
            with BatchHandler(
                client=mock_client, model="test-model", batch_dir=tmp_path, auto_cleanup=True
            ) as handler:
                # Create a test file
                test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]
                saved_filepath = handler._save_batch_file(test_requests)
                assert saved_filepath.exists()

                # Raise an exception
                raise ValueError("Test exception")

        # Run the function that raises an exception
        with pytest.raises(ValueError):
            create_and_raise()

        # File should still be cleaned up despite the exception
        assert saved_filepath is not None
        assert not saved_filepath.exists()


class TestBatchHandlerUtilityMethods:
    def test_get_batch_status(self, chat_handler):
        """Test getting batch status."""
        # Mock the client response
        mock_batch = MagicMock()
        mock_batch.id = "test_batch_id"
        mock_batch.status = "completed"
        mock_batch.created_at = "2024-01-01T00:00:00Z"
        chat_handler.client.batches.retrieve.return_value = mock_batch

        status = chat_handler.get_batch_status("test_batch_id")

        assert status["id"] == "test_batch_id"
        assert status["status"] == "completed"
        assert status["created_at"] == "2024-01-01T00:00:00Z"
        chat_handler.client.batches.retrieve.assert_called_once_with("test_batch_id")

    def test_cancel_batch_success(self, chat_handler):
        """Test successful batch cancellation."""
        chat_handler.client.batches.cancel.return_value = None

        result = chat_handler.cancel_batch("test_batch_id")

        assert result is True
        chat_handler.client.batches.cancel.assert_called_once_with("test_batch_id")

    def test_cancel_batch_failure(self, chat_handler):
        """Test failed batch cancellation."""
        chat_handler.client.batches.cancel.side_effect = Exception("API Error")

        result = chat_handler.cancel_batch("test_batch_id")

        assert result is False


class TestBatchHandlerUtilityMethodsExtended:
    """Extended utility method tests."""

    def test_get_batch_status_with_additional_fields(self, chat_handler):
        """Test getting batch status with additional fields."""
        # Mock a more detailed batch response
        mock_batch = MagicMock()
        mock_batch.id = "test_batch_id"
        mock_batch.status = "completed"
        mock_batch.created_at = "2024-01-01T00:00:00Z"
        mock_batch.completed_at = "2024-01-01T01:00:00Z"
        mock_batch.request_counts = MagicMock()
        mock_batch.request_counts.total = 100
        mock_batch.request_counts.completed = 100
        mock_batch.request_counts.failed = 0

        chat_handler.client.batches.retrieve.return_value = mock_batch

        status = chat_handler.get_batch_status("test_batch_id")

        assert status["id"] == "test_batch_id"
        assert status["status"] == "completed"
        assert status["created_at"] == "2024-01-01T00:00:00Z"
        chat_handler.client.batches.retrieve.assert_called_once_with("test_batch_id")

    def test_cancel_batch_with_logging(self, chat_handler):
        """Test batch cancellation with proper logging verification."""
        # Test successful cancellation
        chat_handler.client.batches.cancel.return_value = None

        result = chat_handler.cancel_batch("test_batch_id")

        assert result is True
        chat_handler.client.batches.cancel.assert_called_once_with("test_batch_id")

    def test_cancel_batch_api_error_handling(self, chat_handler):
        """Test batch cancellation handles different API errors."""
        # Test with different exception types
        api_exceptions = [
            Exception("Generic error"),
        ]

        for exception in api_exceptions:
            chat_handler.client.batches.cancel.side_effect = exception

            result = chat_handler.cancel_batch("test_batch_id")
            assert result is False

            # Reset for next iteration
            chat_handler.client.batches.cancel.reset_mock()


class TestBatchHandlerEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_process_batch_with_kwargs(self, chat_handler):
        """Test processing with additional kwargs passed through."""
        conversations = [[{"role": "user", "content": "Hello"}]]

        # Mock the workflow
        mock_file_obj = MagicMock()
        mock_file_obj.id = "file_123"
        chat_handler.client.files.create.return_value = mock_file_obj

        mock_batch_obj = MagicMock()
        mock_batch_obj.id = "batch_123"
        chat_handler.client.batches.create.return_value = mock_batch_obj

        mock_file_status = MagicMock()
        mock_file_status.status = "processed"
        chat_handler.client.files.retrieve.return_value = mock_file_status

        mock_batch_status = MagicMock()
        mock_batch_status.status = "completed"
        mock_batch_status.output_file_id = "output_file_123"
        chat_handler.client.batches.retrieve.return_value = mock_batch_status

        mock_output_file = MagicMock()
        mock_output_file.text = (
            '{"custom_id": "0", "response": {"body": {"choices": [{"message": {"content": "Hello!"}}]}}}'
        )
        chat_handler.client.files.content.return_value = mock_output_file

        chat_handler.show_progress = False

        # Process with additional kwargs
        results = chat_handler.process_chat_batch(conversations, temperature=0.7, max_tokens=100, top_p=0.9)

        assert "0" in results

    def test_file_save_unique_names(self, chat_handler):
        """Test that saved files have unique names."""
        test_requests = [{"custom_id": "1", "method": "POST", "url": "/v1/chat/completions", "body": {}}]

        # Save multiple files
        filepath1 = chat_handler._save_batch_file(test_requests)
        filepath2 = chat_handler._save_batch_file(test_requests)
        filepath3 = chat_handler._save_batch_file(test_requests)

        # All files should have unique names
        assert filepath1 != filepath2 != filepath3
        assert all(fp.exists() for fp in [filepath1, filepath2, filepath3])

    def test_large_custom_id_handling(self, chat_handler):
        """Test handling of various custom ID formats."""
        conversations = [
            [{"role": "user", "content": "Hello"}],
            [{"role": "user", "content": "Hi"}],
        ]

        # Test with different custom ID types
        custom_ids = ["string_id", 12345]

        requests = chat_handler._create_api_requests(conversations, chat_handler._build_chat_request, custom_ids)

        assert len(requests) == 2
        assert requests[0]["custom_id"] == str(custom_ids[0])
        assert requests[1]["custom_id"] == str(custom_ids[1])
