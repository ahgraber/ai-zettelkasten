"""Unit tests for file utilities."""

import pytest

from aizk.utilities.file_utils import to_valid_fname


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
