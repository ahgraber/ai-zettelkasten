"""Graph-stage persistence and contextualization for converted Markdown artifacts.

This package sits between the pure :mod:`aizk.chunking` splitter and the (separate)
mention-extraction stage. It persists the splitter's chunks durably and
idempotently, then summarizes each document and contextualizes each chunk so the
corpus is a replayable substrate rather than recomputed-and-discarded output.

The pipeline reads a conversion Markdown artifact, runs ``aizk.chunking.split``
in-process, persists the resulting chunks under a chunking run, and then
produces a per-document summary and a per-chunk contextualized revision. Chunk
content rows are immutable; invalidation is expressed only
as a run-status transition on the shared ``pipeline_runs`` primitive, never as a
row mutation.

Storage and the run / dataset-version primitive are shared with the conversion
stage: graph tables live in the conversion SQLite database and the run record
plus transition-event log come from :mod:`aizk.pipeline`.

Process entry points that run this stage set a descriptive ``setproctitle`` at
their own boundary, mirroring :mod:`aizk.conversion`.
"""
