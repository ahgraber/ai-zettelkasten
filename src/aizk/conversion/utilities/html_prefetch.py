"""Pre-fetch ``<img src>`` URLs through the egress gate and rewrite HTML to point at workspace-local copies.

Run before HTML is handed to Docling so that Docling's HTML backend can be
configured with ``enable_remote_fetch=False`` (no outbound HTTP from the
converter) while still rendering images. Each ``<img src>`` URL is fetched
through ``egress_fetch_bytes`` (deny-list policy + connection pinning +
redirect-loop hygiene), saved at ``<workspace>/prefetched-images/<sha256>.<ext>``,
and the ``src`` attribute is rewritten to the absolute local path.

On any per-image failure or once a per-document cap is hit, the offending
``<img src>`` is left unchanged. Docling's ``enable_remote_fetch=False`` plus
the workspace-confinement gate then refuses to dereference it, so the image
drops out of the converted output instead of becoming an exfiltration vector.

See ``.specs/changes/network-egress-policy/design.md`` § "Image pre-fetch +
HTML rewrite for converter".
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import time
from typing import Final
from urllib.parse import urlparse

import lxml.html

from aizk.conversion.core.errors import EgressPolicyError, FetchTooLargeError
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes
from aizk.utilities.url_utils import sanitize_url_for_log

logger = logging.getLogger(__name__)

# Default prefetch policy values. Operator overrides flow through
# ``ConversionConfig`` (env aliases ``PREFETCH_*``) which reads these defaults
# via ``ConversionConfig.prefetch_policy()``. Single source of truth.
_DEFAULT_PER_IMAGE_MAX_BYTES: Final[int] = 10 * 1024 * 1024
_DEFAULT_MAX_IMAGES_PER_DOC: Final[int] = 50
_DEFAULT_MAX_TOTAL_BYTES_PER_DOC: Final[int] = 100 * 1024 * 1024
_DEFAULT_PHASE_DEADLINE_SECONDS: Final[float] = 60.0
_DEFAULT_MAX_IMAGES_PER_HOST: Final[int] = 10


@dataclass(frozen=True)
class PrefetchPolicy:
    """Per-document caps for the ``<img src>`` prefetch phase.

    Centralises the prefetch knobs so callers (the converter, the adapter,
    the egress utility) cannot drift on default values.  ``ConversionConfig``
    holds the env-var operator surface; ``ConversionConfig.prefetch_policy()``
    builds an instance of this dataclass from those fields.

    Attributes:
        per_image_max_bytes: Hard cap on response body bytes per image,
            streaming-enforced inside ``egress_fetch_bytes``.
        max_images: Maximum number of images to prefetch per document.
        max_total_bytes: Maximum cumulative bytes prefetched per document.
        phase_deadline_seconds: Wall-clock budget for the entire prefetch phase.
        max_images_per_host: Per-hostname cap on prefetched images per document.
            Defends against amplification attacks that fan out 50 ``<img>``
            tags pointing at a single victim host.  Counted on successful
            fetch only, so failures do not consume the cap.
    """

    per_image_max_bytes: int = _DEFAULT_PER_IMAGE_MAX_BYTES
    max_images: int = _DEFAULT_MAX_IMAGES_PER_DOC
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES_PER_DOC
    phase_deadline_seconds: float = _DEFAULT_PHASE_DEADLINE_SECONDS
    max_images_per_host: int = _DEFAULT_MAX_IMAGES_PER_HOST


_IMAGES_SUBDIR: Final[str] = "prefetched-images"

_CONTENT_TYPE_TO_EXT: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
_DEFAULT_EXT: Final[str] = ".bin"


def _extension_for_content_type(ct_header: str | None) -> str:
    """Return the file extension for ``ct_header`` from the fixed allowlist.

    Strips Content-Type parameters (``; charset=...``) before lookup. The URL
    path is intentionally NOT consulted as an extension source — it is
    attacker-controlled and could carry path separators or quote characters.
    Any Content-Type outside the allowlist (or absent) falls back to ``.bin``.
    """
    if not ct_header:
        return _DEFAULT_EXT
    primary = ct_header.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_TO_EXT.get(primary, _DEFAULT_EXT)


async def _fetch_and_save_one(
    src: str,
    images_dir: Path,
    *,
    per_image_max_bytes: int,
) -> tuple[Path, int]:
    """Fetch one ``<img src>`` and save it under ``images_dir``; return the path and byte count.

    Raises ``EgressPolicyError``, ``FetchTooLargeError``, ``OSError``, or
    ``FetchError`` on failure so the caller can classify each skip by failure
    mode.

    Args:
        src: Absolute URL of the image to fetch.
        images_dir: Directory to write the fetched image into.
        per_image_max_bytes: Per-image byte cap forwarded to ``egress_fetch_bytes``.

    Returns:
        Tuple of ``(local_path, body_byte_count)`` for the saved image file.
        Returning the count avoids a redundant ``stat`` after the write.

    Raises:
        EgressPolicyError: When the egress policy rejects the URL.
        FetchTooLargeError: When the response body exceeds ``per_image_max_bytes``.
        OSError: When the local write fails (disk full, permission denied, etc.).
        FetchError: For other network failures.
    """
    body, response_headers = await egress_fetch_bytes(
        src,
        max_response_bytes=per_image_max_bytes,
    )
    ext = _extension_for_content_type(response_headers.get("content-type"))
    digest = hashlib.sha256(body).hexdigest()
    local_path = images_dir / f"{digest}{ext}"
    local_path.write_bytes(body)
    return local_path, len(body)


async def prefetch_images(
    html: str,
    workspace: Path,
    *,
    policy: PrefetchPolicy | None = None,
) -> str:
    """Pre-fetch every ``<img src>`` in ``html`` and rewrite each to a workspace-local path.

    ``data:`` URLs are passed through unchanged (no fetch). All other ``src``
    values flow through the egress-validated fetch helper, get content-addressed
    by sha256, and are written to ``workspace/prefetched-images/<sha256>.<ext>``.
    The rewritten ``src`` is the absolute path of that local copy.

    Per-document caps come from ``policy`` (default :class:`PrefetchPolicy`):
        * ``policy.max_images`` images max (default 50)
        * ``policy.max_total_bytes`` total prefetched bytes (default 100 MiB)
        * ``policy.phase_deadline_seconds`` wall-clock budget (default 60 s)
        * ``policy.per_image_max_bytes`` per-image byte cap (default 10 MiB)

    Once any cap is hit (or any individual image fails), the remaining
    ``<img src>`` values are left as-is. The downstream Docling configuration
    (``enable_remote_fetch=False`` + workspace-confinement gate) then refuses
    to dereference them, so the offending images drop out of the output
    rather than becoming SSRF / blind-LFI primitives.

    A per-conversion summary is logged at INFO level at the end of the phase
    with per-failure-mode counts.

    Args:
        html: Source HTML string.
        workspace: Per-job workspace directory. ``prefetched-images/`` is
            created underneath if it does not exist.
        policy: Per-document caps. ``None`` uses :class:`PrefetchPolicy` defaults.

    Returns:
        Serialized HTML with each successfully prefetched ``<img src>``
        rewritten to its absolute workspace-local path.
    """
    p = policy or PrefetchPolicy()
    doc = lxml.html.fromstring(html)
    images_dir = workspace / _IMAGES_SUBDIR
    images_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + p.phase_deadline_seconds
    fetched_count = 0
    fetched_bytes = 0
    egress_blocked = 0
    too_large = 0
    disk_errors = 0
    errors = 0
    cap_hit_logged = False
    host_cap_logged = False
    images_per_host: dict[str, int] = {}

    for img in doc.iter("img"):
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue

        if fetched_count >= p.max_images:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: image count cap reached; remaining images left as-is",
                    extra={"max_images": p.max_images},
                )
                cap_hit_logged = True
            continue
        if fetched_bytes >= p.max_total_bytes:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: total bytes cap reached; remaining images left as-is",
                    extra={"max_total_bytes": p.max_total_bytes, "fetched_bytes": fetched_bytes},
                )
                cap_hit_logged = True
            continue
        if time.monotonic() > deadline:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: phase deadline reached; remaining images left as-is",
                    extra={"phase_deadline_seconds": p.phase_deadline_seconds},
                )
                cap_hit_logged = True
            continue

        # Per-host cap: defends against an HTML doc whose 50 <img src> tags
        # all point at one victim host (outbound amplification primitive).
        # urlparse is lenient: ill-formed URLs return hostname=None, which
        # we bucket together as the empty-string key — the cap still applies.
        host = (urlparse(src).hostname or "").lower()
        if images_per_host.get(host, 0) >= p.max_images_per_host:
            if not host_cap_logged:
                logger.warning(
                    "prefetch_images: per-host image cap reached; further images for hosts at cap left as-is",
                    extra={"host": host, "max_images_per_host": p.max_images_per_host},
                )
                host_cap_logged = True
            continue

        # Bump on attempt, not success: failed requests (timeouts, 5xx, oversize)
        # still consume outbound capacity against the victim host.
        images_per_host[host] = images_per_host.get(host, 0) + 1

        # Bound by remaining total budget so one oversized image can't overshoot.
        per_image_cap = min(p.per_image_max_bytes, p.max_total_bytes - fetched_bytes)

        safe_src = sanitize_url_for_log(src)
        try:
            local_path, body_size = await _fetch_and_save_one(src, images_dir, per_image_max_bytes=per_image_cap)
        except EgressPolicyError as exc:
            logger.warning(
                "prefetch_images: egress policy rejected image src",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__},
            )
            egress_blocked += 1
            continue
        except FetchTooLargeError as exc:
            logger.warning(
                "prefetch_images: image skipped — response exceeds per-image cap",
                extra={
                    "img_src": safe_src,
                    "error_class": exc.__class__.__name__,
                    "per_image_cap_bytes": p.per_image_max_bytes,
                },
            )
            too_large += 1
            continue
        except OSError as exc:
            # Disk write failures (ENOSPC, EACCES, etc.) — distinct from network
            # failures so operator triage doesn't conflate them.
            logger.warning(
                "prefetch_images: image skipped due to local write error",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__, "errno": exc.errno},
            )
            disk_errors += 1
            continue
        except Exception as exc:
            logger.warning(
                "prefetch_images: image skipped due to unexpected error",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__},
            )
            errors += 1
            continue

        img.set("src", str(local_path.resolve()))
        fetched_count += 1
        fetched_bytes += body_size

    skipped = egress_blocked + too_large + disk_errors + errors
    logger.info(
        "prefetch_images: prefetched %d, skipped %d (egress_blocked=%d, too_large=%d, disk_errors=%d, errors=%d)",
        fetched_count,
        skipped,
        egress_blocked,
        too_large,
        disk_errors,
        errors,
    )

    return lxml.html.tostring(doc, encoding="unicode")


__all__ = ["PrefetchPolicy", "prefetch_images"]
