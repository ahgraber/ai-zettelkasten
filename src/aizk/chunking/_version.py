"""Version and calibrated-default constants for the chunking splitter.

``SPLITTER_VERSION`` is a monotonically increasing integer (matching the
project's ``payload_version`` convention) that is bumped whenever the splitter's
observable output changes for any input. ``DEFAULT_SIZE_BUDGET`` is the
calibrated per-chunk character budget; see ``data/chunking-calibration/findings.md``.
"""

from __future__ import annotations

SPLITTER_VERSION: int = 2
DEFAULT_SIZE_BUDGET: int = 4096
