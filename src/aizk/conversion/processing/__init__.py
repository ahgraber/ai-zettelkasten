"""Conversion processing domain: the unit-of-work modules a worker drives.

Holds the conversion stage's domain code — converter/fetcher/uploader,
subprocess spawn + supervision, source enrichment, the error taxonomy, and the
``run_worker`` entrypoint. The generic engine and the claim/recovery queries
live in :mod:`aizk.pipeline`.
"""
