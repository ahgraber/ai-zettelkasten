"""Wiring package: role-specific runtime builders for the conversion pipeline.

This is the sole package that imports from both ``aizk.conversion.core`` and
``aizk.conversion.adapters``.  All other packages must not cross that boundary.
"""

from aizk.conversion.wiring.api import build_api_runtime
from aizk.conversion.wiring.capabilities import DeploymentCapabilities, Probe
from aizk.conversion.wiring.registrations import register_ready_adapters, validate_chain_closure
from aizk.conversion.wiring.testing import build_test_runtime
from aizk.conversion.wiring.worker import build_worker_runtime

__all__ = [
    "DeploymentCapabilities",
    "Probe",
    "build_api_runtime",
    "build_test_runtime",
    "build_worker_runtime",
    "register_ready_adapters",
    "validate_chain_closure",
]
