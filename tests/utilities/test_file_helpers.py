import asyncio
import os
from pathlib import Path

import aiofiles
import pytest

from aizk.utilities.file_helpers import AtomicWriter


class TestAtomicWrite:
    def test_write_str(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        with AtomicWriter(tmp_path / name, binary_mode=False) as f:
            f.write(content)

        assert Path(tmp_path / name).exists()
        assert (tmp_path / name).read_text() == content

    def test_write_path(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        with AtomicWriter(Path(tmp_path / name), binary_mode=False) as f:
            f.write(content)

        assert Path(tmp_path / name).exists()
        assert (tmp_path / name).read_text() == content

    def test_write_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        with AtomicWriter(tmp_path / name, binary_mode=True) as f:
            f.write(content.encode("utf-8"))

        assert Path(tmp_path / name).exists()
        assert (tmp_path / name).read_text() == content

    def test_write_needs_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        with (
            pytest.raises(TypeError),
            AtomicWriter(tmp_path / name, binary_mode=False) as f,
        ):
            f.write(content.encode("utf-8"))

    def test_write_extra_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is string, binary_mode should be False
        with (
            pytest.raises(TypeError),
            AtomicWriter(tmp_path / name, binary_mode=True) as f,
        ):
            f.write(content)


@pytest.mark.asyncio(loop_scope="function")
class TestAsyncAtomicWrite:
    async def test_write_str(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        async with AtomicWriter(tmp_path / name, binary_mode=False) as f:
            await f.write(content)

        assert Path(tmp_path / name).exists()
        assert (tmp_path / name).read_text() == content

    async def test_write_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        async with AtomicWriter(tmp_path / name, binary_mode=True) as f:
            await f.write(content.encode("utf-8"))

        assert Path(tmp_path / name).exists()
        assert (tmp_path / name).read_text() == content

    async def test_write_needs_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        with pytest.raises(TypeError):
            async with AtomicWriter(tmp_path / name, binary_mode=False) as f:
                await f.write(content.encode("utf-8"))

    async def test_write_extra_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is string, binary_mode should be False
        with pytest.raises(TypeError):
            async with AtomicWriter(tmp_path / name, binary_mode=True) as f:
                await f.write(content)
