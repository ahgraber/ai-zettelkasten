"""Typed errors for the pluggable conversion pipeline."""

from __future__ import annotations

from typing import ClassVar


class FetcherNotRegistered(KeyError):
    """Raised when resolution is requested for a kind with no registered adapter."""

    retryable: ClassVar[bool] = False

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind

    def __str__(self) -> str:
        return f"No fetcher registered for kind {self.kind!r}"


class NoConverterForFormat(KeyError):
    """Raised when no converter is registered for the requested (content_type, name)."""

    retryable: ClassVar[bool] = False

    def __init__(self, content_type: object, name: str) -> None:
        super().__init__(content_type, name)
        self.content_type = content_type
        self.name = name

    def __str__(self) -> str:
        return f"No converter registered for ({self.content_type!r}, {self.name!r})"


class FetcherDepthExceeded(RuntimeError):
    """Raised when the resolver chain exceeds the configured depth cap."""

    retryable: ClassVar[bool] = False

    def __init__(self, depth: int, kind: str) -> None:
        super().__init__(f"Resolver chain depth {depth} exceeded at kind {kind!r}")
        self.depth = depth
        self.kind = kind


class ChainNotTerminated(RuntimeError):
    """Raised at wiring time when declared resolver edges are not all registered.

    Startup-time configuration error; not retryable. Process startup must fail
    before requests are accepted.
    """

    retryable: ClassVar[bool] = False
