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


class ConfigurationError(RuntimeError):  # noqa: N818 — canonical spec name
    """Raised at startup when the deployment configuration is invalid.

    Startup-time error: e.g. IngressPolicy references a kind not registered in
    the FetcherRegistry. Never raised at request-handling time.
    """

    error_code = "configuration_error"
    retryable: ClassVar[bool] = False


class IrreversibleMigrationError(RuntimeError):  # noqa: N818 — canonical spec name
    """Raised when a migration downgrade would destroy data with no pre-migration representation."""

    error_code = "irreversible_migration"
    retryable: ClassVar[bool] = False


class FetchError(Exception):  # noqa: N818 — canonical spec name
    """Base exception for fetch errors. Network errors are typically transient and retryable."""

    error_code = "fetch_error"
    retryable: ClassVar[bool] = True


class BookmarkContentUnavailableError(FetchError):  # noqa: N818 — canonical spec name
    """Raised when a KaraKeep bookmark has no usable content. Permanent (not retryable)."""

    error_code = "bookmark_content_unavailable"
    retryable: ClassVar[bool] = False


class ArxivPdfFetchError(FetchError):  # noqa: N818 — canonical spec name
    """Raised when an arXiv PDF fetch fails."""

    error_code = "arxiv_pdf_fetch_failed"


class GitHubReadmeNotFoundError(FetchError):  # noqa: N818 — canonical spec name
    """Raised when no GitHub README variant is found. Permanent (not retryable)."""

    error_code = "github_readme_not_found"
    retryable: ClassVar[bool] = False


class MissingContentError(FetchError):  # noqa: N818 — canonical spec name
    """Raised when a fetcher returns zero-length content. Permanent (not retryable)."""

    error_code = "missing-content"
    retryable: ClassVar[bool] = False


class FetchTooLargeError(FetchError):  # noqa: N818 — canonical spec name
    """Raised when fetched content exceeds the configured byte limit. Permanent."""

    error_code = "fetch_too_large"
    retryable: ClassVar[bool] = False
