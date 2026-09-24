"""Production :class:`~aizk.graph.workunit.MarkdownSource` over conversion's blob store.

The unit-of-work fetches a converted document's Markdown by its
``conversion_output_id`` locator. This implementation resolves that locator to a
:class:`~aizk.conversion.datamodel.output.ConversionOutput` row (for the blob key
and the recorded markdown hash), then reads the Markdown bytes through an injected
blob reader (the conversion stage's S3 client in production, a fake in tests).
Keeping the blob reader behind a narrow :class:`BlobReader` protocol keeps the
graph stage decoupled from boto3/S3 details and deterministically testable.
"""

from aizk.contextualization.processing.markdown_source import (
    BlobReader,
    ConversionOutputFreshness,
    S3MarkdownSource,
)

__all__ = [
    "BlobReader",
    "ConversionOutputFreshness",
    "S3MarkdownSource",
]
