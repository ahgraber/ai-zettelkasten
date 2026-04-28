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


class DoclingConverter:
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
        """Dispatch ``input`` to the appropriate Docling conversion function."""
        metadata = {"docling_version": _get_docling_version()}

        if input.content_type is ContentType.PDF:
            temp_dir = Path(tempfile.mkdtemp(prefix="docling-pdf-"))
            markdown, figures = convert_pdf(
                input.content,
                temp_dir=temp_dir,
                config=self._config,
            )
            return ConversionArtifacts(markdown=markdown, figures=list(figures), metadata=metadata)

        if input.content_type is ContentType.HTML:
            source_url = input.metadata.get("source_url") if input.metadata else None
            temp_dir = Path(tempfile.mkdtemp(prefix="docling-html-"))
            prefetch_kwargs: dict[str, Any] = {}
            if self._conversion_config is not None:
                prefetch_kwargs = {
                    "prefetch_per_image_max_bytes": self._conversion_config.prefetch_per_image_max_bytes,
                    "prefetch_max_images": self._conversion_config.prefetch_max_images_per_doc,
                    "prefetch_max_total_bytes": self._conversion_config.prefetch_max_total_bytes_per_doc,
                    "prefetch_phase_deadline_seconds": self._conversion_config.prefetch_phase_deadline_seconds,
                }
            markdown, figures = convert_html(
                input.content,
                temp_dir=temp_dir,
                config=self._config,
                source_url=source_url,
                **prefetch_kwargs,
            )
            return ConversionArtifacts(markdown=markdown, figures=list(figures), metadata=metadata)

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
