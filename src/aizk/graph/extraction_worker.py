"""Run the extraction worker with an eagerly constructed NER extractor.

Missing NER dependencies or local model weights fail before the runner starts.
The worker also hosts admission when graph configuration enables it.
"""

from aizk.extraction.wiring.worker import build_extractor, run_extraction_worker

__all__ = ["build_extractor", "run_extraction_worker"]
