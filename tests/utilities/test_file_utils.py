"""Unit tests for file utilities."""

from pyleak import no_task_leaks
import pytest

from aizk.utilities.file_utils import AtomicWriter, to_valid_fname


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inva<lid>:name?.txt", "inva-lid-name-.txt"),
        (" spaced name .", "spaced_name"),
        (".hidden", "hidden"),
    ],
)
def test_to_valid_fname_normalizes_filename(raw, expected):
    assert to_valid_fname(raw) == expected


def test_to_valid_fname_rejects_overlong_names():
    with pytest.raises(ValueError, match="Filename is too long"):
        to_valid_fname("a" * 193)


@pytest.mark.asyncio(loop_scope="function")
async def test_atomic_writer_async_no_task_leaks(tmp_path):
    target = tmp_path / "output.txt"

    async with no_task_leaks(action="raise"), AtomicWriter(target) as handle:
        await handle.write("hello")

    assert target.read_text() == "hello"
