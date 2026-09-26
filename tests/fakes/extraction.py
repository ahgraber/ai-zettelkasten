"""A keyword-matching stand-in for the NER extractor."""

from __future__ import annotations

from collections.abc import Sequence

from aizk.extraction.core.extractors import Detection


class KeywordExtractor:
    """An EntityExtractor that detects every non-overlapping occurrence of the given surface forms."""

    def __init__(self, surface_forms: Sequence[str], *, extractor_version: str = "fixture/v1") -> None:
        """Store the surface forms to find and the version stamped on each run."""
        self.surface_forms = list(surface_forms)
        self.extractor_version = extractor_version

    def extract(self, text: str) -> list[Detection]:
        """Return one Detection per occurrence of each surface form, in span order."""
        detections: list[Detection] = []
        for surface_form in self.surface_forms:
            start = text.find(surface_form)
            while start != -1:
                end = start + len(surface_form)
                detections.append(Detection(surface_form=surface_form, span_start=start, span_end=end))
                start = text.find(surface_form, end)
        return sorted(detections, key=lambda detection: (detection.span_start, detection.span_end))
