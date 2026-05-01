"""Shared async HTTP fetch helpers used by fetcher adapters and workers."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from aizk.conversion.core.errors import ArxivPdfFetchError, EgressPolicyError, FetchError
from aizk.conversion.utilities.arxiv_utils import _arxiv_rate_limiter, arxiv_pdf_url
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes
from karakeep_client.karakeep import KarakeepClient

logger = logging.getLogger(__name__)


_KARAKEEP_ASSET_PATH_PREFIX = "/api/v1/assets/"

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _effective_port(scheme: str, port: int | None) -> int | None:
    """Return ``port`` defaulted from the scheme when it is implicit."""
    if port is not None:
        return port
    return _DEFAULT_PORTS.get(scheme.lower())


def is_karakeep_trusted_asset_url(candidate_url: str, karakeep_base_url: str) -> bool:
    """Return True iff ``candidate_url`` is operator-trusted KaraKeep asset infrastructure.

    The carve-out applies only when the candidate URL exactly matches the
    configured ``karakeep_base_url`` origin (scheme, normalized hostname, and
    effective port) and its path begins with ``/api/v1/assets/``. Lookalike
    hosts, suffix/prefix variants, and same-origin non-asset URLs are treated
    as ordinary outbound URLs and routed through the egress gate.
    """
    if not candidate_url or not karakeep_base_url:
        return False
    try:
        candidate = urlparse(candidate_url)
        base = urlparse(karakeep_base_url)
    except ValueError:
        return False

    if not candidate.scheme or not candidate.hostname:
        return False
    if not base.scheme or not base.hostname:
        return False

    if candidate.scheme.lower() != base.scheme.lower():
        return False
    if candidate.hostname.lower() != base.hostname.lower():
        return False
    if _effective_port(candidate.scheme, candidate.port) != _effective_port(base.scheme, base.port):
        return False

    return candidate.path.startswith(_KARAKEEP_ASSET_PATH_PREFIX)


async def fetch_karakeep_asset(asset_id: str) -> bytes:
    """Fetch asset bytes from KaraKeep by asset ID.

    Egress-policy note: this call uses ``KarakeepClient`` directly against the
    operator-configured ``KARAKEEP_BASE_URL`` and is NOT routed through
    ``egress_fetch_bytes``. The carve-out is intentional — see
    ``.specs/changes/network-egress-policy/design.md`` § "Operator-trusted
    endpoints are carved out of the egress gate". Self-hosted KaraKeep
    deployments routinely run on a private network, which the deny-list
    would otherwise refuse.

    Raises:
        FetchError: If the asset fetch fails.
    """
    try:
        async with KarakeepClient() as client:
            return await client.get_asset(asset_id=asset_id)
    except Exception as exc:
        raise FetchError(f"Failed to fetch KaraKeep asset {asset_id}: {exc}") from exc


async def fetch_arxiv_pdf(arxiv_id: str, config: ConversionConfig) -> bytes:
    """Fetch PDF from arXiv by paper ID through the egress-validated helper.

    Applies the arXiv rate limiter (one request per 5-second window per the
    arXiv API ToS) before issuing the HTTP fetch. The egress helper enforces
    deny-list classification, connection pinning, and redirect-loop hygiene.

    Raises:
        ArxivPdfFetchError: If the PDF fetch fails for non-egress reasons.
        EgressPolicyError: If the destination is denied by the egress policy
            (non-retryable; propagated unchanged).
    """
    logger.info("Fetching arXiv PDF by id: %s", arxiv_id)
    url = arxiv_pdf_url(arxiv_id, use_export_url=True)
    await _arxiv_rate_limiter.acquire()
    try:
        body, _headers = await egress_fetch_bytes(
            url,
            max_response_bytes=config.fetch_max_response_bytes,
        )
        return body
    except EgressPolicyError:
        raise
    except Exception as exc:
        raise ArxivPdfFetchError(f"Failed to fetch arXiv PDF for {arxiv_id}: {exc}") from exc


__all__ = ["fetch_arxiv_pdf", "fetch_karakeep_asset"]
