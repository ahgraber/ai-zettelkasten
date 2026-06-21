"""Shared, stage-independent database foundation.

Owns engine creation, the schema-migration runner, and database configuration so
every stage (conversion, graph, and future stages) depends on a common database
module rather than reaching into another stage. Backend-specific wiring lives
under :mod:`aizk.db.backends`.
"""
