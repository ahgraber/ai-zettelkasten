"""Extract entity mentions from chunk text into run-scoped mention drafts.

The module defines the extractor interface, input selection, deterministic
detection materialization, and pinned spaCy and GLiNER2 implementations. It does
not load optional NER dependencies until a production extractor is constructed.
"""

from aizk.extraction.core.extractors import (
    DEFAULT_GLINER2_LABELS,
    GLINER2_REPO_ID,
    MATERIALIZER_VERSION,
    Detection,
    EntityExtractor,
    ExtractionInput,
    Gliner2Extractor,
    SpacyExtractor,
    extract_chunk_mentions,
    gliner2_extractor_version,
    resolve_extraction_config,
    select_extraction_input,
    spacy_extractor_version,
)

__all__ = [
    "DEFAULT_GLINER2_LABELS",
    "GLINER2_REPO_ID",
    "MATERIALIZER_VERSION",
    "Detection",
    "EntityExtractor",
    "ExtractionInput",
    "Gliner2Extractor",
    "SpacyExtractor",
    "extract_chunk_mentions",
    "gliner2_extractor_version",
    "resolve_extraction_config",
    "select_extraction_input",
    "spacy_extractor_version",
]
