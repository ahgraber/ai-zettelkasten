"""Pre-fetch ``<img src>`` URLs through the egress gate and rewrite HTML to point
at workspace-local copies.

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

import hashlib
import logging
from pathlib import Path
import time
from typing import Final

import lxml.html

from aizk.conversion.core.errors import EgressPolicyError, FetchError, FetchTooLargeError
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes

logger = logging.getLogger(__name__)

_PER_IMAGE_MAX_BYTES: Final[int] = 10 * 1024 * 1024
_MAX_IMAGES_PER_DOC: Final[int] = 50
_MAX_TOTAL_BYTES_PER_DOC: Final[int] = 100 * 1024 * 1024
_PHASE_DEADLINE_SECONDS: Final[float] = 60.0

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


async def _fetch_and_save_one(src: str, images_dir: Path) -> Path | None:
    """Fetch one ``<img src>`` and save it under ``images_dir``; return the local path.

    Returns ``None`` on any failure (egress rejection, size overrun, network
    error). The caller leaves the original ``src`` in place when ``None`` is
    returned so Docling's workspace-confinement gate can reject it later.
    """
    try:
        body, response_headers = await egress_fetch_bytes(
            src,
            max_response_bytes=_PER_IMAGE_MAX_BYTES,
        )
    except (EgressPolicyError, FetchTooLargeError, FetchError) as exc:
        logger.warning(
            "Skipping prefetch for image due to typed failure",
            extra={"img_src": src, "error_class": exc.__class__.__name__},
        )
        return None
    except Exception as exc:
        logger.warning(
            "Skipping prefetch for image due to unexpected error",
            extra={"img_src": src, "error_class": exc.__class__.__name__},
        )
        return None

    ext = _extension_for_content_type(response_headers.get("content-type"))
    digest = hashlib.sha256(body).hexdigest()
    local_path = images_dir / f"{digest}{ext}"
    local_path.write_bytes(body)
    return local_path


async def prefetch_images(html: str, workspace: Path) -> str:
    """Pre-fetch every ``<img src>`` in ``html`` and rewrite each to a workspace-local path.

    ``data:`` URLs are passed through unchanged (no fetch). All other ``src``
    values flow through the egress-validated fetch helper, get content-addressed
    by sha256, and are written to ``workspace/prefetched-images/<sha256>.<ext>``.
    The rewritten ``src`` is the absolute path of that local copy.

    Per-document caps:
        * 50 images max
        * 100 MiB total prefetched bytes
        * 60 s wall-clock budget for the entire prefetch phase

    Once any cap is hit (or any individual image fails), the remaining
    ``<img src>`` values are left as-is. The downstream Docling configuration
    (``enable_remote_fetch=False`` + workspace-confinement gate) then refuses
    to dereference them, so the offending images drop out of the output
    rather than becoming SSRF / blind-LFI primitives.

    Args:
        html: Source HTML string.
        workspace: Per-job workspace directory. ``prefetched-images/`` is
            created underneath if it does not exist.

    Returns:
        Serialized HTML with each successfully prefetched ``<img src>``
        rewritten to its absolute workspace-local path.
    """
    doc = lxml.html.fromstring(html)
    images_dir = workspace / _IMAGES_SUBDIR
    images_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + _PHASE_DEADLINE_SECONDS
    fetched_count = 0
    fetched_bytes = 0

    for img in doc.iter("img"):
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue

        if fetched_count >= _MAX_IMAGES_PER_DOC:
            continue
        if fetched_bytes >= _MAX_TOTAL_BYTES_PER_DOC:
            continue
        if time.monotonic() > deadline:
            continue

        local_path = await _fetch_and_save_one(src, images_dir)
        if local_path is None:
            continue

        img.set("src", str(local_path.resolve()))
        fetched_count += 1
        fetched_bytes += local_path.stat().st_size

    return lxml.html.tostring(doc, encoding="unicode")


__all__ = ["prefetch_images"]
