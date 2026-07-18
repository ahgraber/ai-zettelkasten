"""Stage registrations for the operator console.

Importing this package registers every pipeline stage's descriptor, in the order
the console presents them (dashboard rows, monitor stage selection). The console
routes import this package so the registry is populated before the first request.
"""

from __future__ import annotations

from aizk.console.descriptors import register_stage
from aizk.console.stages import contextualization, extraction

register_stage(contextualization.DESCRIPTOR)
register_stage(extraction.DESCRIPTOR)
