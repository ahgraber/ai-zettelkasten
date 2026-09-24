"""Run the contextualization worker with injected model and Markdown dependencies.

The worker requires a complete model endpoint and hosts the stage admission loop.
Admission remains disabled unless the graph configuration enables it.
"""

from aizk.contextualization.wiring.worker import (
    build_llm_client,
    run_graph_worker,
)

__all__ = [
    "build_llm_client",
    "run_graph_worker",
]
