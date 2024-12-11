from pathlib import Path

import pytest

from rag_zk.datamodel.schema import ScrapeStatus
from rag_zk.extractors.base import ExtractionError, Extractor, ExtractorSettings


class TestExtractor:
    def test_init__name(self):
        extractor = Extractor()
        assert extractor.name == "", "Unexpected Extractor().name"

    def test_init__config(self):
        extractor = Extractor()
        assert extractor.config == ExtractorSettings(), "Unexpected Extractor().config"

    def test_init__out_dir(self):
        extractor = Extractor()

        expected = Path.cwd() / "data"
        assert extractor.out_dir == expected, f"Expected {expected}, got {extractor.out_dir}"

    def test_is_static_file(self):
        assert Extractor.is_static_file("http://this.is/a/test.pdf"), "Failed invocation as classmethod"

        extractor = Extractor()
        assert extractor.is_static_file("test.pdf"), "Failed invocation as instance method"

    def test_validate_download(self):
        assert Extractor.validate_download(Path(__file__)), "Failed invocation as classmethod"

        extractor = Extractor()
        assert extractor.validate_download(Path(__file__)), "Failed invocation as instance method"

    def test_cleanup(self):
        extractor = Extractor()

        assert extractor.cleanup() is None

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

    def test_hash(self):
        cmethod = Extractor.hash(Path(__file__))

        extractor = Extractor()
        imethod = extractor.hash(Path(__file__))

        assert cmethod == imethod, "Hashes of same file are not equivalent."
