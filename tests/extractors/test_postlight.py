import json
from pathlib import Path

import pytest

from aizk.datamodel.schema import ScrapeStatus
from aizk.extractors.base import ExtractionError
from aizk.extractors.postlight_parser import PostlightExtractor, PostlightSettings


class TestExtractor:
    def test_init__name(self):
        extractor = PostlightExtractor()
        assert extractor.name == "postlight-parser", "Unexpected PostlightExtractor().name"

    def test_init__config(self):
        extractor = PostlightExtractor()
        assert extractor.config == PostlightSettings(), "Unexpected PostlightExtractor().config"

    def test_init__out_dir(self):
        extractor = PostlightExtractor()

        assert (
            extractor.out_dir == Path.cwd() / "data" / extractor.name
        ), f"Expected {Path.cwd()}, got {extractor.out_dir}"

    def test_cleanup(self):
        # same as base.Extractor / not implemented
        pass

    def test_run(self):
        extractor = PostlightExtractor()
        extract = extractor.run("https://aimlbling-about.ninerealmlabs.com/blog/")
        # extract from postlight-parser should be
        assert extractor.validate_extract(extract) == ScrapeStatus.COMPLETE

    def test_transform_extract(self):
        # same as base.Extractor
        pass

    def test_validate_extract(self):
        extractor = PostlightExtractor()

        assert (
            actual := extractor.validate_extract(json.dumps({"content": "this a test"}))
        ) == ScrapeStatus.COMPLETE, f"Error: Expected successful validation, got {actual}"

        assert (
            actual := extractor.validate_extract("this a test")
        ) == ScrapeStatus.ERROR, f"Error: Expected failed validation, got {actual}"
