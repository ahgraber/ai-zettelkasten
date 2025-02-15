import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from aizk.utilities.process import BatchHandler


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
        # mkdir=True,
    )


@pytest.fixture
def embeddings_handler(mock_client, tmp_path):
    return BatchHandler(
        client=mock_client,
        model="text-embeddings-3-small-batch",
        endpoint="/v1/embeddings",
        batch_dir=tmp_path,
        filename_prefix="batch",
        # mkdir=True,
    )


class TestBatchHandlerInit:
    def test_default_init(self, mock_client, tmp_path):
        model = "test"
        bh = BatchHandler(
            mock_client,
            model=model,
            batch_dir=tmp_path,
        )
        assert bh.model == model
        assert bh.endpoint == "/v1/chat/completions"
        assert bh.batch_dir.is_dir()
        assert bh.filename_prefix == "batch"

    def test_init_missing_batchdir(self, mock_client, tmp_path):
        model = "test"
        does_not_exist = tmp_path / "fake"
        with pytest.raises(NotADirectoryError):
            _ = BatchHandler(
                mock_client,
                model=model,
                batch_dir=does_not_exist,
            )

    def test_init_make_batchdir(self, mock_client, tmp_path):
        model = "test"
        does_not_exist = tmp_path / "fake"
        _ = BatchHandler(
            mock_client,
            model=model,
            batch_dir=does_not_exist,
            mkdir=True,
        )
        assert does_not_exist.exists()
        assert does_not_exist.is_dir()

    def test_chat_init(self, chat_handler):
        assert chat_handler.model == "gpt-4o-mini-batch"
        assert chat_handler.endpoint == "/v1/chat/completions"
        assert chat_handler.batch_dir.is_dir()
        assert chat_handler.filename_prefix == "batch"

    def test_embeddings_init(self, embeddings_handler):
        assert embeddings_handler.model == "text-embeddings-3-small-batch"
        assert embeddings_handler.endpoint == "/v1/embeddings"
        assert embeddings_handler.batch_dir.is_dir()
        assert embeddings_handler.filename_prefix == "batch"


class TestBatchHandlerBatcher:
    def test_batch_api_calls_single_batch(self, chat_handler):
        jsonl = [{"key": "value"} for _ in range(10)]
        batches = list(chat_handler._batch_api_calls(jsonl))
        assert len(batches) == 1
        assert len(batches[0]) == 10

    def test_batch_api_calls_multiple_batches(self, chat_handler):
        max_records = 100
        chat_handler.max_batch_records = max_records
        jsonl = [{"key": "value"} for _ in range(max_records + 1)]
        batches = list(chat_handler._batch_api_calls(jsonl))
        assert len(batches) == 2
        assert len(batches[0]) == max_records
        assert len(batches[1]) == 1

    def test_batch_api_calls_exact_batch_size(self, chat_handler):
        jsonl = [{"key": "value"} for _ in range(chat_handler.max_batch_records)]
        batches = list(chat_handler._batch_api_calls(jsonl))
        assert len(batches) == 1
        assert len(batches[0]) == chat_handler.max_batch_records

    def test_batch_api_calls_batch_size_limit(self, chat_handler):
        max_size = 100
        chat_handler.max_batch_size = max_size

        large_item = {"key": "v" * 1024 * 1024}  # each item is just over 1MB
        jsonl = [large_item for _ in range(chat_handler.max_batch_size + 1)]
        batches = list(chat_handler._batch_api_calls(jsonl))
        assert len(batches) == 2
        # This is a confusing test:
        # Each 'large_item' row is just over 1MB
        # Therefore, when we string together 100 'large_item', we exceed the max size by a little bit
        # So the math isn't as neat, and we have to shift the last row to the second batch
        assert len(batches[0]) == chat_handler.max_batch_size - 1
        assert len(batches[1]) == 2

    def test_batch_api_calls_empty_input(self, chat_handler):
        jsonl = []
        with pytest.raises(ValueError):
            # _batch_api_calls returns a generator, so we need to instantiate
            list(chat_handler._batch_api_calls(jsonl))


class TestBatchHandlerMakeEmbeddings:
    def test_make_embeddings_batch(self, embeddings_handler):
        inputs = ["text1", "text2"]
        embeddings_handler._save_batch_file = MagicMock()
        embeddings_handler.make_embeddings_batch(inputs)
        assert embeddings_handler._save_batch_file.called

    def test_embeddings_batch_file(self, embeddings_handler):
        inputs = ["text1", "text2"]
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
        embeddings_handler.make_embeddings_batch(inputs)

        batchfile = embeddings_handler.batch_dir / f"{embeddings_handler.file_ids[0]}.jsonl"
        assert batchfile.exists()
        assert batchfile.is_file()

        with batchfile.open("rb") as f:
            lines = [json.loads(line) for line in f]
        assert lines == expected


class TestBatchHandlerMakeChat:
    def test_make_chat_batch(self, chat_handler):
        conversations = [
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of the United States?"},
            ],
        ]
        chat_handler._save_batch_file = MagicMock()
        chat_handler.make_chat_batch(conversations)
        assert chat_handler._save_batch_file.called

    def test_chat_batch_file(self, chat_handler):
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
        chat_handler.make_chat_batch(conversations)

        batchfile = chat_handler.batch_dir / f"{chat_handler.file_ids[0]}.jsonl"
        assert batchfile.exists()
        assert batchfile.is_file()

        with batchfile.open("rb") as f:
            lines = [json.loads(line) for line in f]
        assert lines == expected


def test_upload_batch(embeddings_handler, chat_handler):
    # don't want to mock the call to Azure OpenAI
    pass


def test_submit_batch(embeddings_handler, chat_handler):
    # don't want to mock the call to Azure OpenAI
    pass


class TestCompletionDetection:
    def test_check_batch_completion(self, chat_handler):
        chat_handler.batch_infos = [{"batch_id": str(uuid4()), "batchfile": "test.jsonl", "file_id": str(uuid4())}]
        chat_handler.client.batches.retrieve.return_value.status = "completed"
        result = chat_handler.check_batch_completion()
        assert result is True

    def test_wait_for_batch_completion(self, chat_handler):
        chat_handler.check_batch_completion = MagicMock(return_value=True)
        result = chat_handler.wait_for_batch_completion(sleep=1)
        assert result is True


class TestSaveResults:
    def test_save_batch_results(self, chat_handler):
        chat_handler.batch_infos = [{"batch_id": str(uuid4()), "batchfile": "test.jsonl", "file_id": str(uuid4())}]
        chat_handler.client.batches.retrieve.return_value.model_dump.return_value = {"output_file_id": str(uuid4())}
        chat_handler.client.files.content.return_value.text = "test content"
        chat_handler.save_batch_results()
        assert (chat_handler.batch_dir / "processed_test.jsonl").exists()


class TestCleanup:
    def test_cleanup(self, chat_handler):
        chat_handler.batch_infos = [{"batchfile": "test.jsonl", "file_id": str(uuid4())}]
        with (chat_handler.batch_dir / "test.jsonl").open("w") as f:
            f.write("testing 123")

        chat_handler.cleanup()
        assert len(list(chat_handler.batch_dir.glob("*.jsonl"))) == 0
        assert len(chat_handler.batch_infos) == 0
        assert len(chat_handler.file_ids) == 0
