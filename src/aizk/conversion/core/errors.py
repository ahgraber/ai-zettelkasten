"""Typed errors for the pluggable conversion pipeline."""

from __future__ import annotations

from typing import ClassVar


class FetcherNotRegistered(KeyError):
    """Raised when resolution is requested for a kind with no registered adapter."""

    error_code: ClassVar[str] = "fetcher_not_registered"
    retryable: ClassVar[bool] = False

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind

    def __str__(self) -> str:
        return f"No fetcher registered for kind {self.kind!r}"


class NoConverterForFormat(KeyError):
    """Raised when no converter is registered for the requested (content_type, name)."""

    error_code: ClassVar[str] = "no_converter_for_format"
    retryable: ClassVar[bool] = False

    def __init__(self, content_type: object, name: str) -> None:
        super().__init__(content_type, name)
        self.content_type = content_type
        self.name = name

    def __str__(self) -> str:
        return f"No converter registered for ({self.content_type!r}, {self.name!r})"


class FetcherDepthExceeded(RuntimeError):
    """Raised when the resolver chain exceeds the configured depth cap."""

    error_code: ClassVar[str] = "fetcher_depth_exceeded"
    retryable: ClassVar[bool] = False

    def __init__(self, depth: int, kind: str) -> None:
        super().__init__(f"Resolver chain depth {depth} exceeded at kind {kind!r}")
        self.depth = depth
        self.kind = kind


class ChainNotTerminated(RuntimeError):
    """Raised at wiring time when declared resolver edges are not all registered.

    Startup-time configuration error; not retryable. Process startup must fail
    before requests are accepted.

    Attributes:
        resolver_name: the resolver class whose declaration triggered the error.
        missing_kind: the unregistered kind, or None when the error is a cycle.
        cycle_path: the resolver-kind path forming the cycle, or None otherwise.
    """

    error_code: ClassVar[str] = "chain_not_terminated"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        resolver_name: str | None = None,
        missing_kind: str | None = None,
        cycle_path: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.resolver_name = resolver_name
        self.missing_kind = missing_kind
        self.cycle_path = cycle_path
