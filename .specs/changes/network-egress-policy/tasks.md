# Tasks: network-egress-policy

## Egress helper foundation

- [x] Add typed error classes in `src/aizk/conversion/errors.py` (or the existing conversion error module): `EgressPolicyError` base plus `DenyListDestination`, `DisallowedScheme`, `RedirectEgressViolation` (with `reason: Literal["deny_list", "disallowed_scheme", "scheme_downgrade"]`), `DnsTimeout`, `WorkspaceEscape`.
  Mark all subclasses non-retryable in whatever the existing retry-classification mechanism is.
- [x] Create `src/aizk/conversion/utilities/egress.py` with a `ValidatedDestination` dataclass (`ip: str`, `port: int`, `host: str`, `scheme: str`).
- [x] Implement `_classify_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool` in `egress.py` returning True iff the address is in the deny set.
  Primary gate: `not address.is_global`.
  Layered explicit checks: cloud-metadata IPv4 (`169.254.169.254`), cloud-metadata IPv6 (`fd00:ec2::254`), `100.64.0.0/10`, `0.0.0.0/8`, `64:ff9b::/96` (NAT64), `2002::/16` (6to4).
  Normalize IPv4-mapped IPv6 via `address.ipv4_mapped` before classification.
- [x] Implement `_resolve_with_deadline(host: str, port: int, *, timeout: float = 2.0) -> list[tuple[str, int]]` in `egress.py`: submits `socket.getaddrinfo` to a module-level bounded `ThreadPoolExecutor(max_workers=4)`, joins with `timeout=2.0`.
  On timeout raise `DnsTimeout`.
- [x] Implement `assert_egress_allowed(url: str) -> ValidatedDestination` in `egress.py`: parse URL, reject scheme not in `{"http", "https"}` with `DisallowedScheme`, resolve via `_resolve_with_deadline`, classify every returned address, raise `DenyListDestination` if any address is in the deny set, return `ValidatedDestination` with the first resolved address as `ip`.
- [x] Implement `async_assert_egress_allowed(url: str) -> ValidatedDestination` in `egress.py`: submits `assert_egress_allowed` to a dedicated module-level bounded executor (default 4 threads, separate from the DNS executor and from `asyncio.to_thread`'s default executor).
  Configurable via the conversion settings object.
- [x] Unit tests `tests/aizk/conversion/utilities/test_egress.py`: each deny-list category rejected (loopback v4/v6, RFC1918 each /8/12/16, RFC6598, link-local v4/v6, IPv6 ULA, multicast v4/v6, `0.0.0.0/8`, `255.255.255.255`, cloud-metadata v4/v6, NAT64 embedding `169.254.169.254`, 6to4 embedding `10.0.0.1`, IPv4-mapped IPv6 embedding `127.0.0.1`).
- [x] Unit tests for scheme rejection (`file://`, `data:`, `javascript:`, `gopher://`, `ftp://`).
- [x] Unit test for mixed-resolution: a hostname returning `[public_ip, private_ip]` rejected (any private fails).
- [x] Unit test for DNS timeout: monkey-patch `socket.getaddrinfo` to sleep > 2 s, assert `DnsTimeout` raised within the deadline + small margin.
- [x] Unit test that public IPv4 (`8.8.8.8`) and public IPv6 (`2606:4700:4700::1111`) are accepted; verify `ValidatedDestination.ip` equals the resolved address.
- [x] Unit test that RFC 6890 documentation/test ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`, `198.18.0.0/15`) are classified as deny on the pinned Python version.

## UrlRef construction-time validation

- [x] Add a pydantic field validator (or model validator) on `UrlRef` in the pluggable-pipeline source-ref module that calls `assert_egress_allowed(url)` and raises `ValidationError` on `EgressPolicyError`.
  Sync-only; no event loop.
- [x] Update `UrlRef` docstring to note that egress validation runs at construction time and that `model_construct` bypasses it (per design "model_construct bypass note").
- [x] Unit test: `UrlRef(url="http://169.254.169.254/...")` raises `ValidationError`.
- [x] Unit test: `UrlRef.model_validate({"kind": "url", "url": "http://10.0.0.5/admin"})` raises `ValidationError`.
- [x] Unit test: `KarakeepBookmarkResolver` Step 5 with a `source_url` pointing into the deny set surfaces a typed error and never constructs a `UrlRef`.
  Wire test by feeding bookmark JSON through the resolver entrypoint, not by calling `UrlRef` directly.
- [x] Unit test: `UrlRef(url="https://example.com/path")` succeeds and round-trips through pydantic JSON serialization.
- [x] Audit existing call sites that construct `UrlRef` and confirm none use `model_construct` to skip validation.
  Add a one-line code comment at the `UrlRef` class banning internal `model_construct` use, referencing the design note.

## Connection-pinned httpx transport

- [x] Create `src/aizk/conversion/utilities/egress_transport.py` implementing `EgressPinnedTransport(httpx.AsyncHTTPTransport)`.
  Constructor takes a `ValidatedDestination`; `handle_async_request` swaps `request.url.host` for the validated IP just before delegating to `super().handle_async_request`, then restores the original host on the returned response's request object.
- [x] Configure default TLS context in `egress_transport.py`: `ssl.create_default_context()`, `OP_NO_SSLv3 | OP_NO_TLSv1 | OP_NO_TLSv1_1` set explicitly.
  Certificate verification ON.
  SNI uses the original hostname.
- [x] Unit test: a request to `https://example.com` with `ValidatedDestination(ip="93.184.216.34", ...)` actually dials `93.184.216.34` (mock socket layer, assert connect target).
- [x] Unit test: SNI in the TLS handshake equals the original hostname, not the IP.
- [x] Unit test: certificate verification cannot be disabled — passing `verify=False` to the transport constructor either raises or is ignored (whichever is implemented; assert behavior).
- [x] Unit test: no DNS resolution occurs inside `httpx` after the transport is constructed (mock `socket.getaddrinfo` to raise; assert request still completes against the pinned IP).

## Fetcher integration: manual redirect loop

- [x] Refactor `UrlFetcher` in the conversion pipeline to: (1) call `async_assert_egress_allowed(url)` before any I/O; (2) build an `EgressPinnedTransport` from the result; (3) instantiate `httpx.AsyncClient(transport=..., follow_redirects=False, timeout=httpx.Timeout(connect=5.0, read=30.0, pool=5.0, write=30.0))`; (4) implement a redirect loop with hard cap 5 hops and total wall-clock budget 120 s.
- [x] Inside the redirect loop in `UrlFetcher`: on 3xx, parse `Location`, resolve relative against the previous URL via `urljoin`, re-run `async_assert_egress_allowed`, raise `RedirectEgressViolation(reason="deny_list" | "disallowed_scheme")` on failure, raise `RedirectEgressViolation(reason="scheme_downgrade")` if previous scheme was `https` and new scheme is `http`.
- [x] Header hygiene in the redirect loop: when target hostname differs from the preceding hop, strip `Authorization`, `Cookie`, and any header matching `X-*-Auth*` (case-insensitive).
- [x] Apply the same refactor to `ArxivFetcher` (it currently shares the `httpx.AsyncClient(follow_redirects=True)` pattern).
- [x] Apply the same refactor to any other fetcher in the registry that issues outbound HTTP.
  Confirm coverage by enumerating fetcher implementations.
- [x] Unit test: redirect from public host to `169.254.169.254` raises `RedirectEgressViolation(reason="deny_list")`; the redirect target is never fetched.
- [x] Unit test: redirect from `https://a.example` to `http://a.example` raises `RedirectEgressViolation(reason="scheme_downgrade")`.
- [x] Unit test: 6th redirect hop raises (cap is 5 hops).
- [x] Unit test: relative `Location: /next` resolves correctly against the previous absolute URL.
- [x] Unit test: cross-host redirect strips `Authorization` header; same-host redirect preserves it.
- [x] Unit test: DNS-rebinding scenario — initial DNS resolves to public IP, second resolution would return private IP, but the connection-pinned transport still uses the captured public IP.
  Assert no private-IP connection ever attempted.

## Image pre-fetch and HTML rewrite

- [x] Add `lxml` to the project dependencies if not already present (verify via `pyproject.toml`).
- [x] Create `src/aizk/conversion/utilities/html_prefetch.py` implementing `prefetch_images(html: str, workspace: Path, fetcher: <egress-pinned httpx client>) -> str`.
  Parses HTML with `lxml.html`, walks `<img src>` elements, skips `data:` URLs unmodified, calls `async_assert_egress_allowed(src)` for each remaining src, fetches via the pinned transport, writes bytes to `<workspace>/prefetched-images/<sha256>.<ext>`, rewrites the `src` attribute to the absolute path, returns serialized HTML.
- [x] Implement Content-Type → extension mapping in `html_prefetch.py`: `image/png` → `.png`, `image/jpeg` → `.jpg`, `image/gif` → `.gif`, `image/webp` → `.webp`, `image/svg+xml` → `.svg`, anything else → `.bin`.
  Do NOT derive extension from URL path.
- [x] Implement streaming size cap in `html_prefetch.py`: per-image 10 MiB enforced by counting bytes off `response.aiter_bytes()`; abort fetch and skip image on overrun.
  Do NOT trust `Content-Length`.
- [x] Implement per-document caps in `html_prefetch.py`: max 50 images, max 100 MiB total prefetched bytes, 60 s phase wall-clock deadline.
  Once any cap is hit, remaining images are left as original `src` (which Docling's workspace-confinement gate will then reject).
- [x] Wire `prefetch_images` into the converter pipeline before HTML is handed to Docling.
  Locate the call site by searching for `enable_remote_fetch` references in the existing converter code.
- [x] Unit test: HTML with `<img src="http://169.254.169.254/...">` — pre-fetch raises typed egress error for that src, image omitted, conversion of remaining HTML proceeds.
- [x] Unit test: HTML with `<img src="data:image/png;base64,...">` — passthrough; no fetch attempted.
- [x] Unit test: HTML with a public `<img src>` returning 11 MiB — fetch aborted at 10 MiB, image omitted.
- [x] Unit test: HTML with 51 `<img>` tags — first 50 prefetched, 51st left as original src.
- [x] Unit test: rewrite produces `<img src="<absolute-workspace-path>">` and the file at that path matches the fetched bytes.
- [x] Unit test: `Content-Type: text/html` response (a server lying about type) produces extension `.bin`.

## Docling integration: remote-fetch coverage and workspace confinement

- [ ] Implementation-blocking verification test in `tests/aizk/conversion/test_docling_remote_fetch_coverage.py`: with `enable_remote_fetch=False`, exercise HTML containing each of `<link>`, `<script>`, `<img srcset>`, `<picture><source>`, CSS `url(...)`, inline SVG `<image href>`, `<iframe>`.
  Assert no outbound request is attempted (mock the network layer to fail loudly on any connect).
  If any tag type fires, add a scrub step in `html_prefetch.py` to strip those tags before Docling ingestion.
- [ ] Implementation-time spike: write a one-shot test that invokes Docling's local-fetch path with `local_fetch_root=workspace` and a path of `workspace / "../../etc/passwd"`.
  If it raises or returns empty, document this in a code comment and use `local_fetch_root` directly.
  If it does not, fall back to subclassing.
- [ ] Record the spike outcome as a comment in the converter code at the local-fetch wiring site (one short line: `# spike: local_fetch_root enforces containment` OR `# spike: local_fetch_root does NOT enforce containment; using HTMLDocumentBackend subclass`).
- [ ] If the spike fallback is needed: subclass `HTMLDocumentBackend` in `src/aizk/conversion/utilities/docling_backend.py`, override the local-path resolution method to call `_assert_within(workspace, path)` before delegating to `super()`.
  Pass via `backend_class=...` to the document converter.
- [ ] Wire Docling invocation with `enable_remote_fetch=False`, `enable_local_fetch=True`, and `local_fetch_root=workspace` (or the subclass equivalent).
- [ ] Regression test: with the production wiring, an HTML document containing `<img src="/etc/ssh/ssh_host_rsa_key">` triggers `WorkspaceEscape` (or the spike-determined equivalent containment error).
- [ ] Regression test: with the production wiring, an HTML document referencing a workspace-local pre-fetched image (e.g., `/<workspace>/prefetched-images/<sha>.png`) is converted successfully.

## Path-containment helper for subprocess metadata

- [ ] Add `_assert_within(workspace: Path, name: str) -> Path` to `src/aizk/conversion/utilities/paths.py`: string-level pre-check rejects names containing `/`, `\`, `..`, or absolute paths; composes `workspace / name`; calls `.resolve()`; asserts `is_relative_to(workspace.resolve())`; returns the validated absolute path.
  Raises `WorkspaceEscape` on any failure.
- [ ] Refactor `markdown_path` (and any sibling helper) in `paths.py` to delegate to `_assert_within`.
- [ ] Refactor `figure_paths` (or whatever helper consumes `figure_files` from subprocess metadata) to apply `_assert_within` per filename.
- [ ] Update parent-uploader call sites that open subprocess-produced files to use `os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)` with a `with os.fdopen(fd, "rb")` context.
  Catch `OSError` with `errno == ELOOP` and re-raise as `WorkspaceEscape`.
- [ ] Locate uploader call sites by grepping for callers of `markdown_path` / `figure_paths` and update each to the `O_NOFOLLOW` open pattern.
- [ ] Unit tests `tests/aizk/conversion/utilities/test_paths_containment.py`: `name="../../etc/hostname"` rejected; `name="/etc/hostname"` rejected; `name="..\\..\\etc\\hostname"` rejected; `name="output.md"` accepted with composed absolute path; `name="figure-001.png"` accepted.
- [ ] Unit test: workspace contains a symlink `escape -> /etc`; `_assert_within(workspace, "escape/hostname")` raises `WorkspaceEscape` because resolved target falls outside.
- [ ] Unit test: caller opens a path validated by `_assert_within`, then the file is replaced by a symlink to `/etc/passwd`, then the caller opens with `O_NOFOLLOW` — `OSError` with `ELOOP` is caught and re-raised as `WorkspaceEscape`.
- [ ] Unit test: standard subprocess metadata `{"markdown_filename": "output.md", "figure_files": ["figure-001.png", "figure-002.png"]}` flows through the parent uploader without containment errors.

## Error handling, logging, and API hygiene

- [ ] Wire `EgressPolicyError` (and all subclasses) into the job-failure pipeline so the job is failed with non-retryable classification.
  Locate the existing classification site by searching for the precedent error (e.g., `NoConverterForFormat`).
- [ ] Add internal logging at `WARNING` level when any `EgressPolicyError` is raised: include the rejected destination (URL, hostname, resolved IP, redirect chain).
  Use the existing structured logger.
- [ ] Sanitize the `error_message` field persisted to `conversion_jobs` and any API response body: include only the policy-violation class name (e.g., `"deny_list"`, `"disallowed_scheme"`, `"scheme_downgrade"`, `"workspace_escape"`, `"dns_timeout"`), never the rejected destination.
- [ ] Audit every security-policy enforcement site introduced by the security-audit work — across BOTH the `network-egress-policy` change and the already-shipped `deployment-trust-model` change — and ensure each emits a coherent diagnostic WARNING in addition to the audit log.
  Enforcement sites in scope:
  - `network-egress-policy`: `UrlRef` construction-time egress check; `assert_egress_allowed` / `async_assert_egress_allowed`; `egress_fetch_bytes` redirect-loop rejections (deny-list-on-hop, `disallowed_scheme`, `scheme_downgrade`, hop-cap exhaustion, total-budget exhaustion, missing-`Location`); `egress_fetch_bytes` body size cap (`FetchTooLargeError`); `prefetch_images` per-image cap and per-document caps (count / total-bytes / phase-deadline); `_assert_within` workspace-escape rejections at both the subprocess-metadata seam and the converter local-fetch seam.
  - `deployment-trust-model`: `AIZK_AUTH_MODE` startup validation rejection; trusted-host allowlist rejection (Starlette `TrustedHostMiddleware`, HTTP 400); the migration NOT-NULL pre-alter assertion that raises `IrreversibleMigrationError` on unbackfilled rows.
    Diagnostic requirements per emit site: distinguish failure mode in the message (don't collapse multiple modes to one generic line); carry the rejected URL / path / host / IP / hostname / auth-mode value as a structured field; for size-cap failures include both the configured cap and the actually-observed size; for `prefetch_images` emit a per-conversion summary line (`"prefetched N, skipped M (X too-large, Y deny-list, Z network-error)"`) at the end of the phase; for `TrustedHostMiddleware` rejections log the offending `Host` header value (since the request is rejected before it reaches a route handler that could log it itself).
    Goal: an operator reading worker / API logs after a security-policy failure can answer "which policy fired, on what input, with what magnitude" without re-running the request under instrumentation.
- [ ] Promote the `prefetch_images` per-image byte cap (currently the constant `_PER_IMAGE_MAX_BYTES = 10 MiB` in `html_prefetch.py`) and the per-document caps to operator-configurable settings on the conversion config (default to current values), so deployments that legitimately need larger images can raise the cap without a code change.
- [ ] Unit test: a job submitted with a deny-set URL fails with non-retryable classification and the persisted `error_message` does not contain the rejected URL or IP.
- [ ] Unit test: the structured WARNING log entry for the same job DOES contain the rejected destination (verify via log capture).
- [ ] Unit test: an oversized `<img>` prefetch produces a WARNING that names `FetchTooLargeError`, the URL, the configured cap, and the observed-size bound (verify via log capture).
- [ ] Unit test: the per-conversion `prefetch_images` summary line is emitted exactly once per `convert_html` call and reflects the per-failure-mode counts.

## End-to-end / integration

- [ ] E2E test: submit a KaraKeep bookmark whose `source_url` is `http://169.254.169.254/latest/meta-data/`.
  Assert resolver fails before fetcher dispatch, no outbound request is issued, job is non-retryable.
- [ ] E2E test: submit a job whose URL returns a 302 redirect to `http://10.0.0.5/admin`.
  Assert hop is rejected, job is non-retryable, redirect target never fetched.
- [ ] E2E test: submit a job whose URL returns HTML with `<img src="http://169.254.169.254/foo.png">` and one valid public `<img src>`.
  Assert the cloud-metadata image is omitted, the public image is pre-fetched into workspace, conversion succeeds, output references the workspace-local copy.
- [ ] E2E test: simulate a malicious subprocess that emits `metadata.json` with `markdown_filename="../../etc/hostname"`.
  Assert parent uploader rejects with `WorkspaceEscape`, no `open()` is issued against the traversal path, no S3 upload is attempted.
- [ ] E2E test: happy path — submit a public URL whose content has only public-host images.
  Assert end-to-end conversion succeeds and S3 upload completes.

## Documentation

- [ ] Add a short section to `src/aizk/conversion/README.md` (or equivalent docs entry point) describing the egress policy, deny-list categories, and the trust seam between the conversion subprocess and parent uploader.
  Link to the spec.
- [ ] Add a comment block at the top of `egress.py` summarizing the deny set categories and pointing readers to the design's "IP classification library" decision.
