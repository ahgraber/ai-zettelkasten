"""Typed errors for the conversion core with retryability classification."""

from __future__ import annotations

from typing import ClassVar, Sequence


class FetcherNotRegistered(LookupError):  # noqa: N818 — canonical spec name
    """Raised when dispatch is attempted for a kind with no registered adapter."""

    error_code = "fetcher_not_registered"
    retryable: ClassVar[bool] = False


class NoConverterForFormat(LookupError):  # noqa: N818 — canonical spec name
    """Raised when no converter is registered for a (content_type, name) combination."""

    error_code = "no_converter_for_format"
    retryable: ClassVar[bool] = False


class FetcherDepthExceeded(RuntimeError):  # noqa: N818 — canonical spec name
    """Raised when a resolver chain exceeds the configured depth cap."""

    error_code = "fetcher_depth_exceeded"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        cap: int,
        kinds_traversed: Sequence[str],
        config_key: str,
    ) -> None:
        self.cap = cap
        self.kinds_traversed = tuple(kinds_traversed)
        self.config_key = config_key
        message = (
            f"Resolver chain exceeded depth cap of {cap} "
            f"(kinds traversed: {list(self.kinds_traversed)}). "
            f"Raise the cap via configuration key {config_key!r}."
        )
        super().__init__(message)


class ChainNotTerminated(RuntimeError):  # noqa: N818 — canonical spec name
    """Raised at startup when resolver chain closure validation fails.

    Startup-time error: either a resolver declares a `resolves_to` kind that is
    not registered, the resolver DAG contains a cycle, or a declared path
    exceeds the configured depth cap. Never raised at request-handling time.
    """

    error_code = "chain_not_terminated"
    retryable: ClassVar[bool] = False


class RegistrationRoleMismatch(TypeError):  # noqa: N818 — canonical spec name
    """Raised at startup when an adapter is registered under the wrong role.

    Startup-time error: `register_content_fetcher` was called with an impl that
    satisfies `RefResolver`, or `register_resolver` was called with an impl
    that does not satisfy `RefResolver`.
    """

    error_code = "registration_role_mismatch"
    retryable: ClassVar[bool] = False
