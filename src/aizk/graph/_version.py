"""Behavior-version constants for the graph stage's LLM passes.

``SUMMARY_VERSION`` and ``CONTEXT_VERSION`` are monotonically increasing
integers (matching the project's ``payload_version`` / ``SPLITTER_VERSION``
convention). Each is bumped whenever the observable output of its pass changes
for unchanged inputs — a new prompt, a model-tier change, or any framing change
that alters what the pass produces. The version participates in the run's
derivation key, so a bump opens a new run that supersedes the prior one even
when the source content is unchanged.
"""

from __future__ import annotations

SUMMARY_VERSION: int = 1
CONTEXT_VERSION: int = 1
