"""Build the graph operator FastAPI application.

The lifespan loads object-storage, auth, and admission settings into
``app.state`` for route dependencies.
"""

from aizk.console.app import create_app, lifespan

__all__ = ["create_app", "lifespan"]
