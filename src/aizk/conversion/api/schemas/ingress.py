"""Narrow ingress-side source ref union for public API submission."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from aizk.conversion.core.source_ref import KarakeepBookmarkRef

# At cutover: IngressSourceRef admits only KarakeepBookmarkRef.
# Widening is a deliberate public-contract change coordinated with IngressPolicy.
IngressSourceRef = Annotated[KarakeepBookmarkRef, Field(discriminator="kind")]

__all__ = ["IngressSourceRef"]
