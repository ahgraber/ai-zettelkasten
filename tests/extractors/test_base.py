from pathlib import Path

import pytest

from aizk.datamodel.schema import ScrapeStatus
from aizk.extractors.base import ExtractionError, Extractor, ExtractorSettings
from aizk.extractors.utils import atomic_write


class TestAtomicWrite:
    def test_write_str(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        with atomic_write(tmp_path / name, is_binary=False) as f:
            f.write(content)

        assert (tmp_path / name).read_text() == content
        assert len(list(tmp_path.iterdir())) == 1

    def test_write_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, is_binary should be True
        with atomic_write(tmp_path / name, is_binary=True) as f:
            f.write(content.encode("utf-8"))

        assert (tmp_path / name).read_text() == content
        assert len(list(tmp_path.iterdir())) == 1

    def test_write_needs_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, is_binary should be True
        with (
            pytest.raises(TypeError),
            atomic_write(tmp_path / name, is_binary=False) as f,
        ):
            f.write(content.encode("utf-8"))

    def test_write_extra_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is string, is_binary should be False
        with (
            pytest.raises(TypeError),
            atomic_write(tmp_path / name, is_binary=True) as f,
        ):
            f.write(content)


class TestExtractor:
    def test_init__name(self):
        extractor = Extractor()
        assert extractor.name == "", "Unexpected Extractor().name"

    def test_init__config_default(self):
        extractor = Extractor()
        assert extractor.config == ExtractorSettings(), "Unexpected Extractor().config"

    def test_init__config_custom(self):
        settings = ExtractorSettings(timeout=999)
        extractor = Extractor(config=settings)
        assert extractor.config == settings, "Unexpected Extractor().config"

    def test_init__out_dir_default(self):
        extractor = Extractor()

        expected = Path.cwd() / "data"
        assert extractor.out_dir == expected, f"Expected {expected}, got {extractor.out_dir}"

    def test_init__out_dir_custom(self, tmp_path):
        extractor = Extractor(out_dir=tmp_path)
        assert extractor.out_dir == tmp_path, f"Expected {tmp_path}, got {extractor.out_dir}"

    def test_is_static_file(self):
        assert Extractor.is_static_file("http://this.is/a/test.pdf"), "Failed invocation as classmethod"

        extractor = Extractor()
        assert extractor.is_static_file("test.pdf"), "Failed invocation as instance method"

    def test_validate_download(self):
        assert Extractor.validate_download(Path(__file__)), "Failed invocation as classmethod"

        extractor = Extractor()
        assert extractor.validate_download(Path(__file__)), "Failed invocation as instance method"

    def test_cleanup(self):
        pass  # noop / not implemented

    def test_run(self):
        extractor = Extractor()
        with pytest.raises(NotImplementedError):
            extractor.run("http://this.is/a/test")

    def test_transform_extract(self):
        extractor = Extractor()
        extract = "this a test"
        expected = "this a test"  # this is a noop for Extractor()
        assert extractor.transform_extract(extract) == expected

    def test_validate_extract(self):
        extractor = Extractor()
        extract = "this a test"
        expected = ScrapeStatus.COMPLETE
        assert extractor.validate_extract(extract) == expected

    def test_save(self, tmp_path):
        extractor = Extractor(out_dir=tmp_path)
        file_path = tmp_path / "test.txt"
        content = "this is only a test"

        extractor.save(extract=content, file_path=file_path)

        assert file_path.read_text() == content
        assert len(list(tmp_path.iterdir())) == 1

    def test_hash(self):
        cmethod = Extractor.hash(Path(__file__))

        extractor = Extractor()
        imethod = extractor.hash(Path(__file__))

        assert cmethod == imethod, "Hashes of same file are not equivalent."
