"""Integration tests for egress-policy enforcement across the conversion pipeline.

Tests the key security properties end-to-end through real code paths, with mocks
only at the network boundary (socket, HTTP transport, S3).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import queue as queue_module
import socket
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import Session

from aizk.conversion import handler as repository_mod
from aizk.conversion.core.errors import (
    DenyListDestination,
    EgressPolicyError,
    RedirectEgressViolation,
    WorkspaceEscape,
)
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, UrlRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing import subproc, uploader as uploader_mod
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.utilities.egress import ValidatedDestination
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes
from aizk.conversion.utilities.html_prefetch import prefetch_images

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stub_dns(monkeypatch: pytest.MonkeyPatch, host_to_ip: dict[str, str]) -> None:
    """Override socket.getaddrinfo so each host resolves to the given IP."""

    def _fake(host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        ip = host_to_ip.get(host, "8.8.8.8")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


def _mock_transport_factory(
    handler: Callable[[httpx.Request], httpx.Response],
    connected_to: list[str] | None = None,
) -> Callable[[ValidatedDestination], httpx.AsyncBaseTransport]:
    """Return a transport factory backed by an httpx.MockTransport.

    If ``connected_to`` is provided, each ValidatedDestination.ip is appended so
    callers can assert which IPs the transport actually connected to.
    """

    def factory(destination: ValidatedDestination) -> httpx.AsyncBaseTransport:
        if connected_to is not None:
            connected_to.append(destination.ip)
        return httpx.MockTransport(handler)

    return factory


class _InlineProcess:
    """Executes subprocess target inline (same process) for integration testing."""

    pid = None

    def __init__(self, target: Callable, args: tuple) -> None:
        self._target = target
        self._args = args
        self.exitcode: int | None = None

    def start(self) -> None:
        try:
            self._target(*self._args)
            self.exitcode = 0
        except Exception:
            self.exitcode = 1

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


class _InlineContext:
    """Multiprocessing context that runs the subprocess inline."""

    def Queue(self):  # noqa: N802
        return queue_module.Queue()

    def Process(self, target: Callable, args: tuple, daemon: bool) -> _InlineProcess:  # noqa: N802
        return _InlineProcess(target, args)


def _make_fake_runtime() -> MagicMock:
    """Return a WorkerRuntime mock with nullcontext resource_guard."""
    from contextlib import nullcontext

    runtime = MagicMock()
    runtime.resource_guard = nullcontext()
    runtime.capabilities.converter_requires_gpu.return_value = False
    runtime.coordinator = MagicMock()
    return runtime


def _install_memory_s3(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Patch S3Client to write to an in-memory dict instead of real S3."""
    storage: dict[str, bytes] = {}

    def _init(self, config: Any) -> None:
        self.config = config
        self.bucket = config.s3_bucket_name
        self.client = None

    def _upload_file(self, local_path: Path, s3_key: str) -> str:
        storage[s3_key] = local_path.read_bytes()
        return f"s3://{self.bucket}/{s3_key}"

    def _upload_fileobj(self, file_obj: Any, s3_key: str) -> str:
        storage[s3_key] = file_obj.read()
        return f"s3://{self.bucket}/{s3_key}"

    def _get_object_bytes(self, s3_key: str) -> bytes:
        return storage[s3_key]

    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.__init__", _init)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.upload_file", _upload_file)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.upload_fileobj", _upload_fileobj)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.get_object_bytes", _get_object_bytes)
    return storage


# ---------------------------------------------------------------------------
# Test 1: KaraKeep bookmark with a deny-list source_url
# ---------------------------------------------------------------------------


def test_karakeep_resolver_emits_urlref_then_fetcher_blocks_deny_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end deny-list rejection happens at fetch time, not at resolver time.

    Per `network-egress-policy/design.md` § "Defer egress validation to fetch
    time only", `UrlRef` construction does NOT call the egress validator. The
    KaraKeep resolver therefore succeeds at producing a `UrlRef` whose URL
    targets a deny-set destination; the load-bearing rejection happens when
    `UrlFetcher.fetch` dispatches to `egress_fetch_bytes`.

    Security assertions:
    - The deny-set destination is never opened as a socket connection.
    - The fetcher raises an EgressPolicyError (DenyListDestination) at dispatch.
    - The error type is non-retryable so the job fails permanently.
    """
    from karakeep_client.models import ContentTypeLink

    private_source_url = "http://internal.corp.local/meta-data/"

    # DNS: internal.corp.local → 169.254.169.254 (link-local, deny-listed)
    _stub_dns(monkeypatch, {"internal.corp.local": "169.254.169.254"})

    fake_link_content = MagicMock(spec=ContentTypeLink)
    fake_link_content.url = private_source_url
    fake_link_content.html_content = "<html><body>content</body></html>"
    fake_link_content.pdf_asset_id = None
    fake_link_content.precrawled_archive_asset_id = None

    fake_bookmark = MagicMock()
    fake_bookmark.id = "bm_ssrf_test"
    fake_bookmark.content = fake_link_content
    fake_bookmark.assets = []

    # Import the karakeep module first so monkeypatch resolves against the
    # already-loaded module's namespace (rather than dotted-path import).
    from aizk.conversion.adapters.fetchers import karakeep as karakeep_module
    from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
    from aizk.conversion.adapters.fetchers.url import UrlFetcher
    from aizk.conversion.utilities.config import ConversionConfig

    monkeypatch.setattr(karakeep_module, "fetch_karakeep_bookmark", lambda _id: fake_bookmark)

    karakeep_cfg = KarakeepFetcherConfig(_env_file=None)
    resolver = KarakeepBookmarkResolver(karakeep_cfg)
    ref = KarakeepBookmarkRef(bookmark_id="bm_ssrf_test")

    # Resolver SHALL succeed under the revised model — egress validation moved to fetch time.
    resolved, _ = resolver.resolve(ref)
    from aizk.conversion.core.source_ref import UrlRef
    from aizk.conversion.core.types import SourceMetadata

    assert isinstance(resolved, UrlRef)
    # `normalize_url` strips the trailing slash; identity normalization happens at construction.
    assert resolved.url == private_source_url.rstrip("/")

    # Now drive the fetcher; it MUST fail closed at egress validation before any
    # socket connect to the deny-set destination.
    config = ConversionConfig(_env_file=None)
    fetcher = UrlFetcher(config, karakeep_cfg)
    with pytest.raises(EgressPolicyError):
        fetcher.fetch(resolved, SourceMetadata())


# ---------------------------------------------------------------------------
# Test 2: URL with 302 redirect to a private IP
# ---------------------------------------------------------------------------


def test_redirect_to_private_ip_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 302 redirect to a private IP is rejected and the target is never fetched.

    The manual redirect loop in egress_fetch_bytes re-validates each hop.  A
    redirect to http://10.0.0.5/admin must raise RedirectEgressViolation before
    any transport connection to 10.0.0.5 is opened.
    """
    # example.com resolves to a public IP; 10.0.0.5 is a literal private IP
    _stub_dns(monkeypatch, {"example.com": "93.184.216.34"})

    connected_ips: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        # First hop returns redirect to private IP
        return httpx.Response(302, headers={"location": "http://10.0.0.5/admin"})

    factory = _mock_transport_factory(_handler, connected_to=connected_ips)

    with pytest.raises(RedirectEgressViolation):
        asyncio.run(
            egress_fetch_bytes(
                "http://example.com/page",
                max_response_bytes=10 * 1024 * 1024,
                transport_factory=factory,
            )
        )

    # The private redirect target must never have been connected to
    assert "10.0.0.5" not in connected_ips, (
        f"Transport must not connect to private redirect target; connected to: {connected_ips}"
    )


# ---------------------------------------------------------------------------
# Test 3: HTML with mixed img src — private skipped, public prefetched
# ---------------------------------------------------------------------------


def test_prefetch_skips_private_image_and_rewrites_public(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private image is silently dropped; public image is prefetched and its src rewritten.

    After prefetch_images:
    - <img src="http://169.254.169.254/foo.png"> is skipped (egress denied);
      the original src is left in place (not rewritten) so that
      enable_remote_fetch=False prevents Docling from fetching it.
    - <img src="http://public.example.com/valid.png"> is fetched into the
      workspace, and the output HTML's src is rewritten to the workspace-local
      absolute path.
    """
    html = (
        "<html><body>"
        '<img src="http://169.254.169.254/foo.png">'
        '<img src="http://public.example.com/valid.png">'
        "</body></html>"
    )
    # 169.254.169.254 is link-local; public.example.com → public IP
    _stub_dns(monkeypatch, {"public.example.com": "93.184.216.34"})

    # Minimal valid PNG bytes (8-byte PNG signature)
    _png_stub = b"\x89PNG\r\n\x1a\n"

    async def _mock_fetch(url: str, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
        if "169.254.169.254" in url:
            raise DenyListDestination(f"Deny-list address in {url!r}")
        return _png_stub, {"content-type": "image/png"}

    monkeypatch.setattr(
        "aizk.conversion.utilities.html_prefetch.egress_fetch_bytes",
        _mock_fetch,
    )

    output_html = asyncio.run(prefetch_images(html, tmp_path))

    # Private image: original src unchanged (not rewritten)
    assert "169.254.169.254" in output_html, (
        "Private image src must not be rewritten (left for enable_remote_fetch=False to block)"
    )

    # Public image: rewritten to workspace-local absolute path
    prefetched = list((tmp_path / "prefetched-images").glob("*.png"))
    assert len(prefetched) == 1, f"Expected exactly one prefetched image; got {prefetched}"
    local_path = prefetched[0]
    assert str(local_path) in output_html, (
        f"Output HTML must reference the workspace-local copy {local_path}; got html:\n{output_html}"
    )
    assert local_path.read_bytes() == _png_stub


# ---------------------------------------------------------------------------
# Test 4: Malicious subprocess metadata — WorkspaceEscape before S3 upload
# ---------------------------------------------------------------------------


def test_malicious_metadata_raises_workspace_escape_before_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    """A metadata.json with a path-traversal markdown_filename raises WorkspaceEscape.

    The uploader reads markdown_filename from metadata.json and passes it to
    markdown_path(), which calls _assert_within().  A traversal component must
    be rejected before any open() or S3 upload is attempted.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "metadata.json").write_text(
        json.dumps(
            {
                "pipeline_name": "html",
                "terminal_ref": {"kind": "url", "url": "https://example.com"},
                "content_type": "html",
                "markdown_filename": "../../etc/hostname",
                "figure_files": [],
                "markdown_hash_xx64": "deadbeef00000001",
                "docling_version": "test",
                "config_snapshot": {"converter_name": "docling"},
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "source_meta": {},
                "document_title": None,
                "source_title": None,
            }
        )
    )

    upload_calls: list[str] = []

    class _NeverCalledS3:
        bucket = "test-bucket"

        def upload_file(self, local_path: Path, s3_key: str) -> str:
            upload_calls.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

        def upload_fileobj(self, f: Any, s3_key: str) -> str:
            upload_calls.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

    monkeypatch.setattr(uploader_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(uploader_mod, "S3Client", lambda _cfg: _NeverCalledS3())

    config = ConversionConfig(_env_file=None)

    with pytest.raises(WorkspaceEscape):
        uploader_mod._upload_converted(job_id=9999, workspace=workspace, config=config)

    assert upload_calls == [], (
        f"S3 upload must not be attempted when workspace escape is detected; got upload_calls: {upload_calls}"
    )


# ---------------------------------------------------------------------------
# Test 5: Happy path — public URL succeeds end-to-end
# ---------------------------------------------------------------------------


def _make_subprocess_stub(markdown: str = "# Converted output") -> Callable:
    """Return a _process_job_subprocess stub writing pre-built workspace artifacts."""

    def _stub(job_id: int, workspace_path: str, source_ref_json: str, status_queue: Any) -> None:
        from aizk.conversion.utilities.hashing import compute_markdown_hash
        from aizk.conversion.utilities.paths import OUTPUT_MARKDOWN_FILENAME, metadata_path
        from aizk.conversion.utilities.whitespace import normalize_whitespace

        workspace = Path(workspace_path)
        normalized = normalize_whitespace(markdown)
        (workspace / OUTPUT_MARKDOWN_FILENAME).write_text(normalized)

        metadata = {
            "pipeline_name": "html",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
            "figure_files": [],
            "markdown_hash_xx64": compute_markdown_hash(normalized),
            "docling_version": "test",
            "config_snapshot": {"converter_name": "docling"},
            "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_egress_happy"},
            "content_type": "html",
            "source_meta": {},
            "document_title": None,
            "source_title": None,
        }
        metadata_path(workspace).write_text(json.dumps(metadata))

        if status_queue:
            status_queue.put_nowait({"event": "phase", "message": "converting"})
            status_queue.put_nowait({"event": "completed", "message": "done"})

    return _stub


def test_happy_path_public_url_succeeds(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    """A job with a public URL source_ref succeeds end-to-end and S3 upload completes.

    UrlRef construction validates the URL via assert_egress_allowed; a public
    hostname resolves to a public IP and is permitted.  The full pipeline then
    completes: subprocess produces workspace artifacts, uploader writes to S3.
    """
    # Ensure example.com resolves to a public IP so UrlRef construction passes
    _stub_dns(monkeypatch, {"example.com": "93.184.216.34"})

    ref = UrlRef(kind="url", url="https://example.com/article")
    source = Source(
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id="self",
        url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Happy Path",
        content_type="html",
        source_type="web",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)

    # Seed already RUNNING — the adapter's ``execute`` is entered after the claim.
    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        owner_id="self",
        title="Happy Path",
        status=ConversionJobStatus.RUNNING,
        attempts=1,
        idempotency_key="h" * 64,
        source_ref=ref.model_dump_json(),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    storage = _install_memory_s3(monkeypatch)
    monkeypatch.setattr(subproc.mp, "get_context", lambda _ctx: _InlineContext())
    monkeypatch.setattr(subproc, "_process_job_subprocess", _make_subprocess_stub())
    monkeypatch.setattr(subproc, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)
    monkeypatch.setattr(uploader_mod, "get_engine", lambda _url=None: db_session.get_bind())

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    handler.execute(job.id)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, job.id)
    assert updated is not None
    assert updated.status == ConversionJobStatus.SUCCEEDED, (
        f"Expected SUCCEEDED; got {updated.status} / error: {updated.error_message}"
    )
    assert any("output.md" in key for key in storage), (
        f"Expected markdown uploaded to S3; got keys: {list(storage.keys())}"
    )
