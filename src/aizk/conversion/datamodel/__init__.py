"""Datamodel exports for conversion service SQLModel metadata registration."""

from aizk.conversion.datamodel.events import ConversionEventKind
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
import aizk.pipeline  # noqa: F401 — registers pipeline tables on the shared SQLModel.metadata

__all__ = [
    "Source",
    "ConversionJob",
    "ConversionJobStatus",
    "ConversionOutput",
    "ConversionEventKind",
]
