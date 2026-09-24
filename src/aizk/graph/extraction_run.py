"""Extract and persist one source's mentions under an active extraction run.

The module provides currentness classification and incremental and bulk entry
points. Read and extraction work occur before the short write transaction.
Unchanged derivation keys reuse the active run; changed inputs create a
superseding run. The extraction and co-occurrence rows commit together.
"""

from aizk.extraction.core.applicability import record_extraction_run_applicability
from aizk.extraction.core.currentness import (
    Currentness,
    classify_extraction_source,
    classify_extraction_sources,
    stale_extraction_sources,
)
from aizk.extraction.processing.corpus import extract_corpus
from aizk.extraction.processing.document import DocumentExtractionResult, extract_document, extract_source
from aizk.graph.mentions import extraction_derivation_key

__all__ = [
    "Currentness",
    "DocumentExtractionResult",
    "classify_extraction_source",
    "classify_extraction_sources",
    "extract_corpus",
    "extract_document",
    "extract_source",
    "extraction_derivation_key",
    "record_extraction_run_applicability",
    "stale_extraction_sources",
]
