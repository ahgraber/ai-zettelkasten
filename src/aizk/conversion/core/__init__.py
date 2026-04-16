"""Core protocols, types, and registries for the pluggable conversion pipeline.

This package defines the *ports* of the hexagonal conversion architecture.
Concrete adapters live in `aizk.conversion.adapters`; wiring lives in
`aizk.conversion.wiring`. The core package never imports either.
"""
