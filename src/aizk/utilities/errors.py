"""Startup-time errors raised from any layer of the tree.

They live in the leaf package so the engine, the provider clients, and the
service settings can all raise them without importing a package above
themselves, and so every ``except`` clause anywhere sees the same class object.
"""

from __future__ import annotations

from typing import ClassVar


class StartupValidationError(RuntimeError):
    """Raised when a required service is unreachable at startup."""


class ConfigurationError(RuntimeError):  # noqa: N818 — canonical spec name
    """Raised at startup when the deployment configuration is invalid.

    Startup-time error: e.g. IngressPolicy references a kind not registered in
    the FetcherRegistry. Never raised at request-handling time.
    """

    error_code = "configuration_error"
    retryable: ClassVar[bool] = False
