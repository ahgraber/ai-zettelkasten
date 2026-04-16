"""DoclingConverter adapter implementing the Converter protocol."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import ClassVar

from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot


class DoclingConverter:
    """Converter adapter backed by Docling for PDF and HTML content.

    Implements the ``Converter`` protocol from ``aizk.conversion.core.protocols``.
    Delegates byte-level conversion to the existing ``workers.converter`` functions;
    this adapter wraps them in the protocol-compliant class interface.
    """

    supported_formats: ClassVar[frozenset[ContentType]] = frozenset(
        {ContentType.PDF, ContentType.HTML}
    )
    requires_gpu: ClassVar[bool] = True

    def __init__(self, config: ConversionConfig) -> None:
        self._config = config

    def convert(self, conversion_input: ConversionInput) -> ConversionArtifacts:
        # Lazy import avoids a circular dependency: workers.converter will re-export
        # this class, so importing it at module level would create a cycle.
        from aizk.conversion.workers.converter import convert_html, convert_pdf

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            source_url: str | None = str(conversion_input.metadata.get("source_url") or "") or None

            if conversion_input.content_type is ContentType.PDF:
                markdown, figure_paths = convert_pdf(
                    conversion_input.content,
                    workspace,
                    self._config,
                )
            else:
                markdown, figure_paths = convert_html(
                    conversion_input.content,
                    workspace,
                    self._config,
                    source_url=source_url,
                )

            figures = [p.read_bytes() for p in figure_paths]

        return ConversionArtifacts(markdown=markdown, figures=figures)

    def config_snapshot(self) -> dict[str, object]:
        """Return the output-affecting config fields used for idempotency keying."""
        return build_output_config_snapshot(
            self._config,
            picture_description_enabled=self._config.is_picture_description_enabled(),
        )
