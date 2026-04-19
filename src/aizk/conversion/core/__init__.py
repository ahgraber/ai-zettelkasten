"""Core protocols, types, registries, and SourceRef union for the conversion pipeline.

Ports & Adapters boundary: this package defines the abstract interfaces and
data shapes that adapters and orchestrators depend on. It MUST NOT import
from `aizk.conversion.adapters` or from concrete adapter implementations.
"""
