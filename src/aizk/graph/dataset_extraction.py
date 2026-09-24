"""Run a foreground extraction pass over selected sources.

Explicit source lists run without confirmation. Corpus scans require confirmation
and use the active chunking runs to select sources.
"""

from aizk.extraction.processing.corpus import resolve_target_source_ids, run_dataset_extraction

__all__ = ["resolve_target_source_ids", "run_dataset_extraction"]
