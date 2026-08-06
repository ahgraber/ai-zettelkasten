"""Admit ``<img src>`` references through the egress gate and inline the bytes into the HTML.

This is the pipeline's admission boundary for page-referenced resources, and it
runs before the HTML reaches the converter. Each ``<img src>`` is resolved
against the source URL, fetched through ``egress_fetch_bytes`` (deny-list policy
+ connection pinning + redirect-loop hygiene), saved at
``<workspace>/prefetched-images/<sha256>.<ext>`` for on-host inspection, and
carried into the document as a ``data:`` URI.

Carrying the bytes inline rather than as a filesystem path means conversion
performs no local read derived from page content: the converter's ``data:``
branch decodes them without a base path and without touching the filesystem.

A reference that is not admitted — because the egress policy rejected it, or a
per-document cap was reached, or the fetch failed — has its ``src`` attribute
removed, so the converter emits a figure placeholder and performs no I/O for it.
Every non-admission is recorded with the class that caused it, and the
end-of-phase summary accounts for all of them.

Admission reaches ``<img src>`` only. Other resource-bearing shapes a page may
use are covered by the converter being configured so it can dereference no
location at all; see the converter's format-option construction.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import time
from typing import Final
from urllib.parse import urljoin, urlparse

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
            tags pointing at a single victim host.  Counted on attempt, so a
            failed request still consumes the cap — it consumed outbound
            capacity against the host regardless of how it ended.
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

# Media type used for an admitted image whose Content-Type is absent or outside
# the allowlist. It must be under ``image/`` for the converter's ``data:`` branch
# to recognise the payload; the real format is then sniffed from the bytes.
_DEFAULT_MEDIA_TYPE: Final[str] = "image/octet-stream"


def _primary_content_type(ct_header: str | None) -> str | None:
    """Return the lowercased media type from ``ct_header``, or ``None`` if absent.

    Strips Content-Type parameters (``; charset=...``) before returning.
    """
    if not ct_header:
        return None
    return ct_header.split(";", 1)[0].strip().lower()


def _extension_for_content_type(ct_header: str | None) -> str:
    """Return the file extension for ``ct_header`` from the fixed allowlist.

    Strips Content-Type parameters (``; charset=...``) before lookup. The URL
    path is intentionally NOT consulted as an extension source — it is
    attacker-controlled and could carry path separators or quote characters.
    Any Content-Type outside the allowlist (or absent) falls back to ``.bin``.
    """
    primary = _primary_content_type(ct_header)
    if primary is None:
        return _DEFAULT_EXT
    return _CONTENT_TYPE_TO_EXT.get(primary, _DEFAULT_EXT)


def _media_type_for_content_type(ct_header: str | None) -> str:
    """Return the ``data:`` URI media type for ``ct_header`` from the fixed allowlist.

    Uses the same allowlist as :func:`_extension_for_content_type` and, like it,
    never consults the attacker-controlled URL path. Any Content-Type outside the
    allowlist (or absent) falls back to ``image/octet-stream``: the media type
    must be under ``image/`` for the converter to decode the payload, and the
    real format is recovered by sniffing the bytes.
    """
    primary = _primary_content_type(ct_header)
    if primary is not None and primary in _CONTENT_TYPE_TO_EXT:
        return primary
    return _DEFAULT_MEDIA_TYPE


def _as_data_uri(body: bytes, ct_header: str | None) -> str:
    """Return ``body`` encoded as a ``data:`` URI with an ``image/`` media type."""
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{_media_type_for_content_type(ct_header)};base64,{encoded}"


def _resolve_reference(
    img: lxml.html.HtmlElement,
    src: str,
    source_url: str | None,
) -> tuple[str, str] | None:
    """Resolve the location an ``<img>`` names and key it by host.

    Resolution happens here, ahead of the per-host cap, because keying an
    unresolved page-relative reference would bucket every one of them under the
    empty host and trip that cap on a single page's own images.

    Args:
        img: The element being admitted. A ``data:`` reference is normalised in
            place; nothing else is mutated.
        src: The element's ``src``, already stripped.
        source_url: URL the document was fetched from, or ``None``.

    Returns:
        ``(resolved_location, host_key)``, or ``None`` when the element names no
        resource to admit. That covers a blank or fragment-only reference —
        resolving ``#x`` would yield the source URL itself, turning it into a
        fetch of the page whose HTML would then be inlined as image bytes — and a
        ``data:`` URI, which already carries its bytes. The ``data:`` scheme is
        case-insensitive in a browser but the converter matches it
        case-sensitively, so it is normalised rather than merely detected;
        passing ``DATA:`` through verbatim would lose the image inside the
        converter with no record of the loss.

    Raises:
        ValueError: When the reference cannot be parsed.
    """
    if not src or src.startswith("#"):
        return None
    if src.lower().startswith("data:"):
        img.set("src", "data:" + src[len("data:") :])
        return None
    resolved = urljoin(source_url, src) if source_url else src
    # urlparse is lenient about most malformed input: an unrecognisable
    # authority yields hostname=None, which buckets under the empty-string key
    # so the per-host cap still applies.
    return resolved, (urlparse(resolved).hostname or "").lower()


class _LocalWriteError(OSError):
    """Raised when the workspace copy of a fetched image cannot be written.

    Distinguishes a genuine disk failure from the network errors that also
    surface as ``OSError`` (``socket.gaierror`` and ``ConnectionError`` are both
    ``OSError`` subclasses), so the two are not reported to operators under the
    same non-admission class.
    """


async def _fetch_and_save_one(
    src: str,
    images_dir: Path,
    *,
    per_image_max_bytes: int,
) -> tuple[bytes, str | None]:
    """Fetch one ``<img src>``, save a workspace copy, and return the bytes and Content-Type.

    Raises ``EgressPolicyError``, ``FetchTooLargeError``, ``_LocalWriteError``,
    ``OSError``, or ``FetchError`` on failure so the caller can classify each
    non-admission by cause.

    Args:
        src: Absolute URL of the image to fetch.
        images_dir: Directory to write the fetched image into.
        per_image_max_bytes: Per-image byte cap forwarded to ``egress_fetch_bytes``.

    Returns:
        Tuple of ``(body, content_type_header)``. The body is returned so the
        caller can inline it without re-reading the file it was just written to;
        the Content-Type is returned so the caller can label the inline payload
        from the same allowlist that chose the file extension. The workspace copy
        is written for on-host inspection and is not read back.

    Raises:
        EgressPolicyError: When the egress policy rejects the URL.
        FetchTooLargeError: When the response body exceeds ``per_image_max_bytes``.
        _LocalWriteError: When the workspace copy cannot be written.
        OSError: For network failures that surface as ``OSError``.
        FetchError: For other network failures.
    """
    body, response_headers = await egress_fetch_bytes(
        src,
        max_response_bytes=per_image_max_bytes,
    )
    content_type = response_headers.get("content-type")
    ext = _extension_for_content_type(content_type)
    digest = hashlib.sha256(body).hexdigest()
    local_path = images_dir / f"{digest}{ext}"
    try:
        local_path.write_bytes(body)
    except OSError as exc:
        raise _LocalWriteError(f"could not write workspace copy: {local_path}") from exc
    return body, content_type


async def prefetch_images(
    html: str,
    workspace: Path,
    *,
    policy: PrefetchPolicy | None = None,
    source_url: str | None = None,
) -> str:
    """Admit every ``<img src>`` in ``html`` through the egress gate and inline the admitted bytes.

    ``data:`` URIs already carry their bytes, so they are passed through
    unchanged with no fetch and no read. Every other ``src`` is resolved against
    ``source_url``, flows through the egress-validated fetch helper, is
    content-addressed by sha256 into ``workspace/prefetched-images/<sha256>.<ext>``
    for on-host inspection, and is rewritten to a ``data:`` URI carrying the
    fetched bytes.

    Per-document caps come from ``policy`` (default :class:`PrefetchPolicy`):
        * ``policy.max_images`` images max (default 50)
        * ``policy.max_total_bytes`` total prefetched bytes (default 100 MiB)
        * ``policy.phase_deadline_seconds`` wall-clock budget (default 60 s)
        * ``policy.per_image_max_bytes`` per-image byte cap (default 10 MiB)
        * ``policy.max_images_per_host`` per-hostname cap (default 10)

    A reference that is not admitted — because a cap was reached or the fetch
    failed — has its ``src`` attribute removed, so the converter emits a figure
    placeholder and performs no I/O for it. Every non-admission is recorded with
    the class that caused it, and the end-of-phase summary at INFO level counts
    all of them.

    Args:
        html: Source HTML string.
        workspace: Per-job workspace directory. ``prefetched-images/`` is
            created underneath if it does not exist.
        policy: Per-document caps. ``None`` uses :class:`PrefetchPolicy` defaults.
        source_url: URL the document was fetched from, used to resolve
            page-relative references. ``None`` leaves references unresolved, in
            which case relative ones fail egress validation and are dropped.

    Returns:
        Serialized HTML in which every admitted ``<img src>`` carries its bytes
        inline and every non-admitted ``<img>`` has no ``src`` attribute.
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
    cap_dropped = 0
    cap_hit_logged = False
    host_cap_logged = False
    images_per_host: dict[str, int] = {}

    def _not_admitted(element: lxml.html.HtmlElement, reference: str, non_admission_class: str) -> None:
        """Drop ``element``'s ``src`` and record the reference that was not admitted.

        Removing the attribute is what makes the reference unresolvable by the
        converter: it takes its empty-source branch, emits a figure placeholder,
        and performs no I/O. The record is per reference so an operator can tell
        a page that had no images from a page whose images were all dropped.
        """
        element.attrib.pop("src", None)
        logger.debug(
            "prefetch_images: image reference not admitted",
            extra={"img_src": sanitize_url_for_log(reference), "non_admission_class": non_admission_class},
        )

    for img in doc.iter("img"):
        raw_src = (img.get("src") or "").strip()
        try:
            resolved = _resolve_reference(img, raw_src, source_url)
        except ValueError:
            # A malformed authority (unbalanced IPv6 brackets, a bad IPv6
            # literal) must drop one reference, never abort the document.
            logger.warning(
                "prefetch_images: image dropped — reference could not be parsed",
                extra={"img_src": sanitize_url_for_log(raw_src)},
            )
            errors += 1
            _not_admitted(img, raw_src, "malformed_reference")
            continue
        if resolved is None:
            continue
        src, host = resolved

        if fetched_count >= p.max_images:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: image count cap reached; remaining images dropped",
                    extra={"max_images": p.max_images},
                )
                cap_hit_logged = True
            _not_admitted(img, src, "max_images")
            cap_dropped += 1
            continue
        if fetched_bytes >= p.max_total_bytes:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: total bytes cap reached; remaining images dropped",
                    extra={"max_total_bytes": p.max_total_bytes, "fetched_bytes": fetched_bytes},
                )
                cap_hit_logged = True
            _not_admitted(img, src, "max_total_bytes")
            cap_dropped += 1
            continue
        if time.monotonic() > deadline:
            if not cap_hit_logged:
                logger.warning(
                    "prefetch_images: phase deadline reached; remaining images dropped",
                    extra={"phase_deadline_seconds": p.phase_deadline_seconds},
                )
                cap_hit_logged = True
            _not_admitted(img, src, "phase_deadline_seconds")
            cap_dropped += 1
            continue

        if images_per_host.get(host, 0) >= p.max_images_per_host:
            if not host_cap_logged:
                logger.warning(
                    "prefetch_images: per-host image cap reached; further images for hosts at cap dropped",
                    extra={"host": host, "max_images_per_host": p.max_images_per_host},
                )
                host_cap_logged = True
            _not_admitted(img, src, "max_images_per_host")
            cap_dropped += 1
            continue

        # Bump on attempt, not success: failed requests (timeouts, 5xx, oversize)
        # still consume outbound capacity against the victim host.
        images_per_host[host] = images_per_host.get(host, 0) + 1

        # Bound by remaining total budget so one oversized image can't overshoot.
        per_image_cap = min(p.per_image_max_bytes, p.max_total_bytes - fetched_bytes)

        safe_src = sanitize_url_for_log(src)
        try:
            body, content_type = await _fetch_and_save_one(src, images_dir, per_image_max_bytes=per_image_cap)
        except EgressPolicyError as exc:
            logger.warning(
                "prefetch_images: egress policy rejected image src",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__},
            )
            egress_blocked += 1
            _not_admitted(img, src, "egress_policy")
            continue
        except FetchTooLargeError as exc:
            logger.warning(
                "prefetch_images: image dropped — response exceeds per-image cap",
                extra={
                    "img_src": safe_src,
                    "error_class": exc.__class__.__name__,
                    "per_image_cap_bytes": p.per_image_max_bytes,
                },
            )
            too_large += 1
            _not_admitted(img, src, "per_image_max_bytes")
            continue
        except _LocalWriteError as exc:
            # Disk write failures (ENOSPC, EACCES, etc.) — kept distinct from the
            # network failures below, which are also OSError subclasses, so
            # operator triage doesn't conflate the two.
            logger.warning(
                "prefetch_images: image dropped due to local write error",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__, "errno": exc.errno},
            )
            disk_errors += 1
            _not_admitted(img, src, "disk_error")
            continue
        except OSError as exc:
            # Name resolution and connection failures reach here: socket.gaierror
            # and ConnectionError are OSError subclasses.
            logger.warning(
                "prefetch_images: image dropped due to network error",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__, "errno": exc.errno},
            )
            errors += 1
            _not_admitted(img, src, "network_error")
            continue
        except Exception as exc:
            logger.warning(
                "prefetch_images: image dropped due to unexpected error",
                extra={"img_src": safe_src, "error_class": exc.__class__.__name__},
            )
            errors += 1
            _not_admitted(img, src, "unexpected_error")
            continue

        # Carry the bytes inline. The workspace copy at `local_path` stays for
        # on-host inspection of a running job; the converter never reads it.
        img.set("src", _as_data_uri(body, content_type))
        fetched_count += 1
        fetched_bytes += len(body)

    skipped = egress_blocked + too_large + disk_errors + errors + cap_dropped
    logger.info(
        "prefetch_images: admitted %d, not admitted %d "
        "(egress_blocked=%d, too_large=%d, disk_errors=%d, errors=%d, cap_dropped=%d)",
        fetched_count,
        skipped,
        egress_blocked,
        too_large,
        disk_errors,
        errors,
        cap_dropped,
    )

    return lxml.html.tostring(doc, encoding="unicode")


__all__ = ["PrefetchPolicy", "prefetch_images"]
