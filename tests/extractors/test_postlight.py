import json
from pathlib import Path

import pytest

from aizk.datamodel.schema import ScrapeStatus
from aizk.extractors.base import ExtractionError
from aizk.extractors.postlight_parser import PostlightExtractor, PostlightSettings


class TestExtractor:
    def test_init__name(self, tmp_path):
        extractor = PostlightExtractor(out_dir=tmp_path)
        assert extractor.name == "postlight-parser", "Unexpected PostlightExtractor().name"

    def test_init__config(self, tmp_path):
        extractor = PostlightExtractor(out_dir=tmp_path)
        assert extractor.config == PostlightSettings(), "Unexpected PostlightExtractor().config"

    def test_init__out_dir(self, tmp_path):
        # same as base.Extractor / not implemented
        pass

    def test_cleanup(self, tmp_path):
        # same as base.Extractor / not implemented
        pass

    @pytest.mark.asyncio
    async def test_run(self, tmp_path):
        # how to mock without actually doing web requests?
        pass

        # extractor = PostlightExtractor(out_dir=tmp_path)
        # extract = await extractor.run("https://aimlbling-about.ninerealmlabs.com/blog/", out_dir=extractor.out_dir)
        # # extract from postlight-parser should be
        # assert extractor.validate_extract(extract)

    def test_transform_extract(self):
        # same as base.Extractor
        pass

    def test_validate_extract(self, tmp_path):
        extractor = PostlightExtractor(out_dir=tmp_path)

        assert (
            actual := extractor.validate_extract(json.dumps({"content": "this a test"}))
        ), f"Error: Expected successful validation, got {actual}"

        with pytest.raises(ExtractionError):
            extractor.validate_extract("this a test")
