"""Define graph-stage settings loaded from ``AIZK_GRAPH__*`` environment variables.

The settings cover contextualization, extraction, worker lease and retry
behavior, and the local GLiNER2 model revision; the admission section is
:class:`~aizk.pipeline.admission.AdmissionConfig`, re-exported here. A model
endpoint must be complete before contextualization starts; model weights load
from the configured local directory.
"""

from aizk.contextualization.settings import ContextualizationConfig
from aizk.extraction.settings import ExtractionConfig, NerConfig
from aizk.pipeline.admission import AdmissionConfig

__all__ = ["AdmissionConfig", "ContextualizationConfig", "ExtractionConfig", "NerConfig"]
