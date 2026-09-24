"""Compute read-only cold-start statistics for the active corpus mention dataset.

The dataset is the union of mentions and co-occurrences in each source's active
extraction run. Statistics include source, revision, combined, and co-occurrence
partitions and return as a JSON-serializable :class:`DatasetStatistics`.
"""

from aizk.extraction.core.dataset_stats import (
    AnchorClassStatistics,
    ChunkDensityStatistics,
    CooccurrenceClassStatistics,
    CooccurrenceStatistics,
    DatasetStatistics,
    MentionCountStatistics,
    compute_dataset_statistics,
)

__all__ = [
    "AnchorClassStatistics",
    "ChunkDensityStatistics",
    "CooccurrenceClassStatistics",
    "CooccurrenceStatistics",
    "DatasetStatistics",
    "MentionCountStatistics",
    "compute_dataset_statistics",
]
