"""Production :class:`~aizk.graph.workunit.MarkdownSource` over conversion's blob store.

The unit-of-work fetches a converted document's Markdown by its
``conversion_output_id`` locator. This implementation resolves that locator to a
:class:`~aizk.conversion.datamodel.output.ConversionOutput` row (for the blob key
and the recorded markdown hash), then reads the Markdown bytes through an injected
blob reader (the conversion stage's S3 client in production, a fake in tests).
Keeping the blob reader behind a narrow :class:`BlobReader` protocol keeps the
graph stage decoupled from boto3/S3 details and deterministically testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import func
from sqlmodel import Session, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.workunit import LoadedMarkdown

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Engine


@runtime_checkable
class BlobReader(Protocol):
    """Reads an object's raw bytes by storage key.

    The conversion stage's S3 client satisfies this structurally via its
    ``get_object_bytes`` method; tests supply an in-memory double.
    """

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Return the raw bytes stored under ``s3_key``."""
        ...


class S3MarkdownSource:
    """Fetches a conversion output's Markdown from blob storage by locator."""

    def __init__(self, engine: "Engine", blob_reader: BlobReader) -> None:
        """Store the engine (to resolve the output row) and the blob reader."""
        self._engine = engine
        self._blob_reader = blob_reader

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the Markdown text and its recorded hash for a conversion output.

        Resolves the ``ConversionOutput`` row for the blob key and the hash the
        conversion stage recorded, then reads and decodes the Markdown blob. The
        caller (:func:`~aizk.graph.workunit.process_document`) verifies the text
        hashes to ``markdown_hash_xx64``.

        Raises:
            ValueError: If no conversion output exists for the locator.
        """
        with Session(self._engine) as session:
            output = session.get(ConversionOutput, conversion_output_id)
            if output is None:
                raise ValueError(f"conversion output {conversion_output_id} not found")
            markdown_key = output.markdown_key
            markdown_hash = output.markdown_hash_xx64
        data = self._blob_reader.get_object_bytes(markdown_key)
        return LoadedMarkdown(text=data.decode("utf-8"), markdown_hash_xx64=markdown_hash)


class ConversionOutputFreshness:
    """Production :class:`~aizk.graph.workunit.OutputFreshness` over conversion outputs.

    An output is current iff it **belongs to** the supplied source and is that
    source's latest (highest-id, monotonic under the single serialized writer)
    ``ConversionOutput``. Verifying the output row's own ``source_id`` closes an
    identity hole: without it, an output id from a *different* source that happens
    to exceed the supplied source's max id would pass, letting another source's
    Markdown be persisted under the wrong source. The query runs on the persist
    transaction's session, so the identity + freshness check and the supersede are
    atomic.
    """

    def is_current(self, session: Session, source_id: "UUID", conversion_output_id: int) -> bool:
        """Return ``True`` iff the output belongs to ``source_id`` and is its latest."""
        output = session.get(ConversionOutput, conversion_output_id)
        if output is None or output.source_id != source_id:
            return False
        latest_id = session.exec(
            select(func.max(ConversionOutput.id)).where(ConversionOutput.source_id == source_id)
        ).one()
        return conversion_output_id >= latest_id
