"""Docling converter adapter implementing the ``Converter`` protocol.

The adapter is a thin wrapper around the legacy ``convert_pdf`` / ``convert_html``
free functions living in :mod:`aizk.conversion.workers.converter`. The free
functions stay in place for PR 3 — this adapter calls into them. A later PR
will relocate the conversion implementation alongside the adapter.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any, ClassVar, Optional

from aizk.conversion.core.protocols import Converter
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot
from aizk.conversion.workers.converter import convert_html, convert_pdf


def _get_docling_version() -> str:
    """Return installed docling version, or 'unknown' if not found."""
    try:
        from importlib.metadata import version as _importlib_version

        return _importlib_version("docling")
    except Exception:
        return "unknown"


class DoclingConverter(Converter):
    """Converter adapter backed by Docling for PDF and HTML inputs."""

    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})
    requires_gpu: ClassVar[bool] = True

    def __init__(self, config: DoclingConverterConfig, conversion_config: Optional[ConversionConfig] = None) -> None:
        """Initialize with Docling-specific and conversion-level configs.

        Args:
            config: Docling-specific adapter configuration.
            conversion_config: Operator-level conversion configuration used for
                prefetch caps. When ``None``, the per-call defaults in
                ``convert_html`` (matching the module constants) are used.
        """
        self._config = config
        self._conversion_config = conversion_config

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — protocol argument name
        """Dispatch ``input`` to the appropriate Docling conversion function.

        Extracts the first TitleItem from the Docling result as ``document_title``
        (raw observation — no heuristic filtering applied here) and passes
        ``source_meta.document_base_url`` (or ``source_url``) to Docling as the
        ``source=`` parameter so relative links resolve correctly.
        """
        metadata = {"docling_version": _get_docling_version()}
        source_url = input.source_meta.document_base_url or input.source_meta.source_url

        if input.content_type is ContentType.PDF:
            temp_dir = Path(tempfile.mkdtemp(prefix="docling-pdf-"))
            markdown, figures, document_title = convert_pdf(
                input.content,
                temp_dir=temp_dir,
                config=self._config,
            )
            return ConversionArtifacts(
                markdown=markdown,
                figures=list(figures),
                metadata=metadata,
                document_title=document_title,
            )

        if input.content_type is ContentType.HTML:
            temp_dir = Path(tempfile.mkdtemp(prefix="docling-html-"))
            prefetch_policy = (
                self._conversion_config.prefetch_policy() if self._conversion_config is not None else None
            )
            markdown, figures, document_title = convert_html(
                input.content,
                temp_dir=temp_dir,
                config=self._config,
                source_url=source_url,
                prefetch_policy=prefetch_policy,
            )
            return ConversionArtifacts(
                markdown=markdown,
                figures=list(figures),
                metadata=metadata,
                document_title=document_title,
            )

        raise ValueError(
            f"DoclingConverter does not support content_type={input.content_type!r}; "
            f"supported formats are {sorted(ct.value for ct in self.supported_formats)}"
        )

    def config_snapshot(self) -> dict[str, Any]:
        """Return the output-affecting subset of config for idempotency keys.

        Delegates to :func:`build_output_config_snapshot` so the adapter's
        contribution to the idempotency key matches the legacy Docling hash
        field set exactly. Endpoint URL and API key are intentionally excluded
        (they do not affect replayable output). ``converter_name`` is NOT
        added here — the orchestrator tags the snapshot at a higher layer.
        """
        return build_output_config_snapshot(
            self._config,
            picture_description_enabled=self._config.is_picture_description_enabled(),
        )


__all__ = ["DoclingConverter"]
