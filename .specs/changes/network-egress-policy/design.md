# Design: network-egress-policy

## Context

The conversion worker is the only component in the system that issues outbound HTTP requests on behalf of user-submitted content.
Today it does so via `httpx.AsyncClient(follow_redirects=True).stream("GET", url)` with no host filter, no scheme restriction, and no per-redirect-hop validation.
Attacker-controlled inputs reach this code path through the KaraKeep resolver Step 5, which constructs `UrlRef(url=source_url)` from bookmark JSON, and through future widening of `_API_SUBMITTABLE_KINDS` to include `arxiv` / `url` / `github_readme` / `inline_html`.

Constraints shaping the design:

- The conversion API is internal-only at cutover but the project will be published for self-hosted deployment in containers fronted by a reverse proxy.
  The design must hold up under that future without additional work.
- The Pluggable Pipeline contract is already settled.
  This change extends the contract — it does not restructure it.
- Pydantic v2 is the validation layer for `SourceRef` variants; construction-time validation runs synchronously inside `model_validate`.
- The conversion subprocess is launched via `multiprocessing.get_context("spawn")` with `os.setpgrp()`.
  No filesystem or network namespace isolation is in place.
  The design assumes this remains true and compensates at the application layer.
- Docling's HTML backend supports `enable_remote_fetch` and `enable_local_fetch` flags; the flags are coarse — there is no per-call hook to validate URLs before Docling fetches.
  The design works around this rather than depending on Docling cooperation.

## Decisions

### Decision: Egress helper location

**Chosen:** Put the egress validator in `src/aizk/conversion/utilities/egress.py`.

**Rationale:** The validator carries security semantics tied to the conversion deployment model.
Co-locating it with the conversion package keeps the security boundary visible inside the package that owns it, and keeps `aizk/utilities/url_utils.py` strictly syntactic (per the audit's negative-finding N1: that helper is not a security boundary, and conflating the two would invite future callers to assume it is).

**Alternatives considered:**

- `aizk/utilities/egress.py`: rejected because it suggests the helper is general-purpose.
  The deny set and policy are conversion-specific deployment posture, not a project-wide invariant.
- Inlined into `UrlFetcher`: rejected because three call-sites need it (UrlFetcher, ArxivFetcher, UrlRef construction, plus the converter image-prefetch path) and a shared helper avoids drift.

### Decision: IP classification library

**Chosen:** Python stdlib `ipaddress` module.

**Rationale:** No new dependency.
The classification gate uses `not address.is_global` as the primary check — an allowlist on IP-space semantics rather than an enumerated deny list.
This is the safer primitive: any missing deny-range is an exploit; `not is_global` shrinks rather than enumerates the attack surface.

Additional explicit checks are layered on top for ranges not reliably excluded by `is_global` alone:

- Cloud-metadata IPs (`169.254.169.254`, `fd00:ec2::254`) — belt-and-suspenders against `is_link_local` / `is_private` gaps on older Python minor versions; listed explicitly for auditability.
- Shared-address space `100.64.0.0/10` (RFC 6598) — not consistently classified as non-global across all Python versions; checked explicitly via `IPv4Network("100.64.0.0/10")` containment.
- `0.0.0.0/8` (RFC 791 "this network") — `is_unspecified` matches only `0.0.0.0`, not the full /8; checked explicitly via `IPv4Network("0.0.0.0/8")` containment.
- `64:ff9b::/96` (RFC 6052 NAT64) — embeds an IPv4 address in the low 32 bits; neither `ipv4_mapped` (which only normalizes `::ffff:0:0/96`) nor `is_global` fully excludes this range; checked explicitly.
- `2002::/16` (RFC 3056 6to4) — embeds an IPv4 address in bits 17–48; same shape as NAT64 smuggling; checked explicitly.

IPv4-mapped IPv6 addresses (`::ffff:0.0.0.0/96`) are normalized via `ipaddress.ip_address(...).ipv4_mapped` before classification so an attacker cannot smuggle a private IPv4 inside an IPv6 envelope.

The implementation requires Python ≥ 3.11; `is_private` coverage for RFC 6890 documentation/test ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`, `198.18.0.0/15`) is verified against the pinned Python version in unit tests.

**Alternatives considered:**

- `netaddr`: rejected; adds a dependency for marginal ergonomics.
- Enumerated deny-list as primary gate: rejected in favor of `not is_global`; a blocklist can miss ranges.

### Decision: Two helper APIs (sync + async)

**Chosen:** Two functions in `egress.py`:

- `assert_egress_allowed(url: str) -> ValidatedDestination` — synchronous; resolves DNS via `socket.getaddrinfo` with a hard **2-second wall-clock deadline** enforced by submitting the call to a bounded `concurrent.futures.ThreadPoolExecutor` and joining with `timeout=2.0`.
  Deadline expiry raises `DnsTimeout` (a non-retryable `EgressPolicyError` subclass).
  Classifies all resolved addresses; returns `ValidatedDestination(ip: str, port: int, host: str, scheme: str)` or raises a typed `EgressPolicyError`.
  `ip` is the first address in the `getaddrinfo` result set — the address that will be pinned for the socket connection.
- `async_assert_egress_allowed(url: str) -> ValidatedDestination` — async wrapper that submits the sync version to a **dedicated bounded `ThreadPoolExecutor`** (default 4 threads; configurable via the conversion settings object) shared across the event loop lifetime.
  The default `asyncio.to_thread` executor is explicitly rejected here: it is shared with all other blocking calls and can stall the event loop under concurrent DNS fan-out (e.g., 50-image pre-fetch phase).

**Rationale:** The async helper is the load-bearing trust boundary, called from every fetcher / prefetch path.
The sync helper exists for code paths that are themselves synchronous (e.g., `UrlFetcher.fetch` is a sync protocol method that runs `asyncio.run` internally; tests; future utilities).
Both share one classification implementation so the deny-list semantics cannot drift.
The 2-second DNS deadline prevents slow-resolver DoS in the worker.

**Note (post-implementation revision):** the original decision also justified the sync API as a way to run egress validation inside `UrlRef`'s pydantic validator at construction time.
That construction-time call has since been removed — see "Defer egress validation to fetch time only" below.
The sync API remains for fetcher / utility use, but is no longer reachable from API request handling.

**Alternatives considered:**

- Async-only with sync-from-async wrapper: rejected; sync callers (e.g., `UrlFetcher.fetch` operating under the sync `ContentFetcher` protocol) would each need an event-loop bridge, duplicating logic.

### Decision: Connection pinning via custom httpx transport

**Chosen:** `httpx.AsyncHTTPTransport` subclass that, on each connection, takes a pre-validated IP from the destination metadata and dials the IP directly while preserving the URL Host (and TLS SNI) for the request.
Implementation: override `handle_async_request` to swap the URL host with the validated IP just before sending and then restore it on the request object that travels with the response.
The transport is constructed per-fetch with the validated destination already attached — DNS does not run inside `httpx`.
The IP pinned is the first address in the `getaddrinfo` result set returned by `assert_egress_allowed`.

**TLS posture:**

- Certificate verification is ON (`ssl_context=ssl.create_default_context()`); the subclass MUST NOT weaken this (no `verify=False`, no `ssl_context` override that disables verification).
- Minimum TLS 1.2 enforced via the default ssl context (`OP_NO_SSLv3 | OP_NO_TLSv1 | OP_NO_TLSv1_1` — already the Python default; pinned explicitly in context construction).
- Hostname verification uses the original URL hostname (not the pinned IP) so the cert's SAN is checked against the correct name.
- No custom CA bundle; no peer-verify bypass.

**Rationale:** Defeats DNS-rebinding TOCTOU.
The IP that classification approved is the IP the socket connects to.
Host header and SNI continue to use the original hostname so TLS verification works against the cert.

**Alternatives considered:**

- Rewrite the URL to `http://<ip>/...` and override Host header: rejected because TLS SNI breaks; certificate verification fails against an IP-shaped SNI.
- Trust httpx's internal DNS and accept TOCTOU window: rejected; the audit chain explicitly relies on a public-then-private rebinding pattern.

### Decision: Manual redirect loop with per-hop validation

**Chosen:** Set `follow_redirects=False` on `httpx.AsyncClient` and implement a manual redirect loop with a hard cap of 5 hops (matching httpx default).
At each hop: parse the `Location` header, resolve relative→absolute against the previous URL, run the egress validation again, and either rebuild the connection-pinned transport for the new host or raise `RedirectEgressViolation`.

**Per-hop timeouts:** connect 5 s, read 30 s, total per-hop 60 s; total redirect chain wall-clock budget 120 s.

**Header hygiene on cross-host redirect:** when the redirect target hostname differs from the preceding hop, strip `Authorization`, `Cookie`, and any header matching `X-*-Auth*` before issuing the next request.
Same-host redirects retain headers only if the scheme is not downgraded.

**Scheme downgrade policy:** redirects from `https://` to `http://` are rejected and raise `RedirectEgressViolation(reason="scheme_downgrade")`.
All-`http` chains and `http → https` upgrades are accepted.

**Rationale:** A validated initial host can redirect to a private host; without per-hop re-validation the rebinding-style attack works through HTTP redirects instead of DNS.

**Alternatives considered:**

- httpx event hooks on redirect: rejected because hooks fire after the redirect is followed; the connection has already been made.
- Single-validation + reject any 3xx: rejected; legitimate redirects (e.g., GitHub raw → S3 CDN) need to be supported.

### Decision: Image pre-fetch + HTML rewrite for converter

**Chosen:** Before handing HTML to Docling, scan for `<img src>` URLs, run each through `async_assert_egress_allowed`, fetch the bytes via the same connection-pinned transport into the job's workspace at `<workspace>/prefetched-images/<sha256>.<ext>`, then rewrite the HTML's `<img src>` to the absolute path of the local copy.

**Caps:**

- Per-image: 10 MiB, enforced by streaming byte count off the response body.
  `Content-Length` is NOT used to enforce this cap — it is attacker-controllable and unreliable over chunked encoding.
  On overrun the fetch is aborted and the image omitted.
- Per-document: 50 images and 100 MiB total prefetched bytes.
- Pre-fetch phase wall-clock deadline: 60 s.

Run Docling with `enable_remote_fetch=False`, `enable_local_fetch=True`.

**Coverage scope:** the pre-fetch step handles `<img src>` URLs only.
It does NOT rewrite `srcset`, `<picture><source>`, CSS `url()`, inline SVG `<image href>`, or `<link>`/`<script>` — these are left in the HTML as-is.
`enable_remote_fetch=False` is the containment line for all remaining external references.
**Implementation MUST verify that Docling honors `enable_remote_fetch=False` for every resource type it may dereference** (link, script, srcset, CSS font URLs, SVG image, iframe).
If any resource type is not gated by the flag, those URLs must be scrubbed from the HTML before passing to Docling.
This verification is a blocking implementation gate, not a nice-to-have.

**`data:` URL handling:** `<img src="data:...">` values are left unmodified by the pre-fetch step — they carry inline bytes and require no outbound fetch.
The rewriter skips them rather than passing them through the egress gate.

**Extension derivation:** the file extension in `<sha256>.<ext>` is derived from the response `Content-Type` header against a fixed allowlist: `image/png` → `.png`, `image/jpeg` → `.jpg`, `image/gif` → `.gif`, `image/webp` → `.webp`, `image/svg+xml` → `.svg`.
Any `Content-Type` not in this allowlist produces `.bin`.
The URL path is NOT used as the extension source — it is attacker-controlled and can contain quotes or path separators.

**HTML parser:** the pre-fetch rewrite MUST use a battle-tested HTML parser (`lxml.html` preferred; `html.parser` from the stdlib as fallback).
Regex-based `<img>` matching is explicitly rejected: it cannot handle attribute quoting variants, tag nesting, or encoding edge cases correctly.
The parser backend used for the rewrite SHOULD match the backend Docling's HTML backend uses internally, to eliminate parser-differential mutation.

Docling's local-fetch then opens files inside the workspace; the converter-side scenario in the spec asserts that any `<img src>` not pointing at a workspace-relative path is rejected (because pre-fetch failure removes the rewrite, leaving the original out-of-workspace src to be caught by the workspace-confinement gate at Docling-open time).

**Rationale:** Preserves image embedding while routing every fetch through the egress gate and never letting Docling open a path outside the workspace.
HTML rewriting is at the source-of-truth layer — the bytes Docling sees only contain workspace-local refs.

**Alternatives considered:**

- Just disable both flags: rejected per user decision.
- Custom HTTP transport injected into Docling: rejected; Docling does not expose a clean injection point and we'd be patching internals.
- In-memory image embedding (`data:` URLs): rejected; defeats the "no remote fetch" property because relative URLs in nested HTML still need a base, and inflates HTML size beyond the existing 64 KiB `InlineHtmlRef` cap.

### Decision: Workspace-confinement gate for converter local fetches

**Chosen:** A wrapper around Docling's local-fetch path that, before any `open()`, resolves the path and asserts `is_relative_to(workspace.resolve())`.
Monkey-patching `_load_image_data` is rejected.
The mechanism was determined by an implementation-time spike:

**Spike outcome:** `HTMLBackendOptions` has no `local_fetch_root` field.
Docling's `_load_image_data` opens local files with a bare `open(src_loc, "rb")` — no containment check, no field to configure one.
A path like `workspace/../../etc/passwd` is read unconditionally.
The fallback branch was taken.

**Implementation:** `make_confined_backend(workspace)` in `src/aizk/conversion/utilities/docling_backend.py` returns a `HTMLDocumentBackend` subclass that overrides `_load_image_data`.
The override resolves the local path, asserts `is_relative_to(workspace.resolve())`, and raises `WorkspaceEscape` on failure before delegating to `super()`.
The subclass is passed to the document converter as `HTMLFormatOption(backend=make_confined_backend(workspace), ...)`.

**Rationale:** Reuses the path-containment helper from Vuln 3, so both the subprocess-metadata seam and the converter local-fetch seam share one trust-boundary gate.
The spike-first approach defers the mechanism choice to implementation but ensures it is made deliberately and recorded, not discovered silently at code review.

**Alternatives considered:**

- Rely solely on `local_fetch_root` without verifying it enforces containment: rejected; if the option is merely a relative-path base, paths with `..` or absolute prefixes still escape.
- Monkey-patch `_load_image_data` at the module level: rejected; fragile against Docling version bumps and not detectable by static analysis tools.

### Decision: Path containment helper reused at all subprocess seams

**Chosen:** Tighten `markdown_path` and `figure_paths` in `src/aizk/conversion/utilities/paths.py` to call a shared `_assert_within(workspace: Path, name: str) -> Path` helper that:

1. Rejects names containing `/`, `\`, `..`, or that are absolute (string-level pre-check; rejects path separators before any filesystem touch).
2. Composes `workspace / name`, resolves it, and asserts `is_relative_to(workspace.resolve())`.
3. Returns the validated absolute path.

**Callers that open files returned by `_assert_within` MUST use `O_NOFOLLOW`:**

```python
fd = os.open(str(validated_path), os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(fd, "rb") as f:
    content = f.read()
```

`O_NOFOLLOW` causes `os.open` to fail with `ELOOP` if the final path component is a symlink at open time, eliminating the TOCTOU race between the containment check and the open.
`ELOOP` MUST be caught and re-raised as `WorkspaceEscape`.
Re-resolve-and-recheck is explicitly rejected: it still has a race window between the second `resolve()` and the `open()`.

The conversion worker runs on Linux in a container; `O_NOFOLLOW` is available.
Windows is not a supported deployment target for the subprocess layer.

**Rationale:** Single helper used for both subprocess-metadata containment (Vuln 3) and converter local-fetch containment.
`O_NOFOLLOW` at the open site eliminates the TOCTOU gap that `_assert_within` alone cannot close.
A shared helper is a single place to add rules (e.g., reject Unicode normalization tricks) if needed later.

### Decision: Typed errors

**Chosen:** Add `EgressPolicyError` to the conversion error hierarchy with subclasses:

- `DenyListDestination` — resolved IP is in the deny set.
- `DisallowedScheme` — scheme not in `{http, https}`.
- `RedirectEgressViolation` — a redirect hop fails validation (includes `reason` field; values: `"deny_list"`, `"disallowed_scheme"`, `"scheme_downgrade"`).
- `DnsTimeout` — DNS resolution exceeded the 2-second deadline.
- `WorkspaceEscape` — a path containment check fails (used by both subprocess-metadata seam and converter local-fetch seam).

All `EgressPolicyError` subclasses are classified non-retryable.
The error message identifies the policy-violation class but **SHALL NOT echo the rejected destination back into structured output served to clients**.
The rejected destination (URL, hostname, resolved IP) **SHALL be captured in internal logs** at `WARNING` level for SOC/diagnostic use; it must not appear in the `error_message` field persisted to `conversion_jobs` or in any API response body.

**Trust boundary note:** the load-bearing egress check is the fetch-time call to `async_assert_egress_allowed` inside `UrlFetcher` / `ArxivFetcher` / `GithubReadmeFetcher` / `prefetch_images` / `egress_fetch_bytes`'s manual redirect loop.
`UrlRef` construction does no DNS or destination classification — it only normalizes the URL string for stable identity.
See "Defer egress validation to fetch time only" decision below for the rationale and what was removed.

**Rationale:** Aligns with the existing typed-error pattern (`FetcherNotRegistered`, `ChainNotTerminated`, `FetcherDepthExceeded`).
Non-retryable classification matches the `NoConverterForFormat` precedent for non-recoverable input-shape errors.

### Decision: Defer egress validation to fetch time only (post-implementation revision)

**Chosen:** Egress validation runs only at fetch time, inside the worker.
`UrlRef` construction performs URL normalization for dedup identity but does **not** call `assert_egress_allowed` and does **not** resolve DNS.

**What was removed:**

- The `_assert_egress` field validator on `UrlRef` (formerly invoked `assert_egress_allowed` synchronously during `UrlRef` construction / `model_validate`).
- The dependency from `core/source_ref.py` on `utilities/egress.py`.
- The MODIFIED-Requirements clause in `pluggable-pipeline/spec.md` that required `UrlRef` construction to reject deny-set destinations (now reversed).

**What remains the load-bearing trust boundary:**

- `egress_fetch_bytes` calls `async_assert_egress_allowed` for every initial-hop URL.
- The manual redirect loop calls it again for every 3xx target.
- `prefetch_images` routes every `<img src>` through `egress_fetch_bytes` (so per-image egress is gated transitively).
- `KarakeepClient` does not pass through `egress_fetch_bytes` and is intentionally exempt — KaraKeep's base URL is operator-configured trusted infrastructure (see `KarakeepFetcherConfig.base_url`), which the original construction-time check incorrectly fail-closed against in private deployments.

**Rationale:**

1. **Eliminates the KaraKeep private-base-URL paradox.**
   The construction-time check could not distinguish "operator-trusted internal asset URL" from "attacker-supplied private destination" — a `UrlRef(url=f"{karakeep_base_url}/api/v1/assets/{asset_id}")` constructed by `KarakeepBookmarkResolver` Steps 3 and 4 fail-closed against the egress policy whenever KaraKeep is on a private network (the canonical self-hosted deployment shape).
   With validation moved to fetch time, the KaraKeep asset path uses `KarakeepClient` directly and never reaches the deny-list check; non-KaraKeep `UrlRef` instances still get egress validation when their fetcher dispatches.

2. **Removes a synchronous DNS round-trip from API request handling and worker rehydration.**
   Pydantic validators run synchronously in whatever thread is calling `model_validate`.
   In the API path that meant `POST /v1/jobs` could block an event-loop worker for up to 2 s on `getaddrinfo`; in the worker path it meant 3–6 redundant DNS lookups per job (orchestrator parent rehydration, subprocess rehydration, uploader's `terminal_ref` and `submitted_ref` reconstruction, plus every resolver-built `UrlRef`).
   Without rate-limiting on the unauthenticated API endpoint, this was a queryable DNS-resolution oracle and a chokepoint on the 4-thread DNS executor.

3. **Removes the `model_construct` bypass footgun.**
   The previous design admitted that `UrlRef.model_construct(...)` bypassed the validator, and warned internal code not to use it.
   With no validator to bypass, the warning becomes moot.

**Operator-visible behavior change:**

- Previously: a JSON submission with a deny-set URL was rejected at the API with a `pydantic.ValidationError` (HTTP 422-shaped).
- Now: the submission accepts the job; the worker fails the job non-retryably with `EgressPolicyError` when the fetcher dispatches.
  The persisted `error_message` is the policy-violation `error_code` only; the rejected destination is captured in WARNING logs at the enforcement site, never in API responses.
- Net UX: ~immediate rejection becomes "job accepted, fails on first worker pickup."
  For an internal-only deployment this is acceptable; the security property (no outbound request to a deny-set destination) is preserved.

**Alternatives considered:**

- Keep construction-time validation but add an operator allowlist for KaraKeep: rejected.
  Adding a permanent allowlist knob to the egress policy core to work around a paradox the policy itself created has worse trust-boundary ergonomics than dropping the redundant check.
- Surgical bypass via `model_construct` for resolver-built KaraKeep asset URLs: rejected.
  Sets a precedent for internal `model_construct` use that the docstring and design previously discouraged; spreads the bypass across multiple call sites.
- Keep a lexical-only validator (scheme allowlist + hostname presence) without DNS: deferred.
  Possible follow-up if fail-fast UX becomes important; not required for the security property.

### Decision: Operator-trusted endpoints are carved out of the egress gate

**Chosen:** Outbound HTTP to **operator-configured** endpoints — not user-supplied destinations — bypasses `egress_fetch_bytes` by design.
The egress policy's threat model is "untrusted URL from a job submission reaches a deny-set destination."
For URLs whose target is read from operator configuration (env vars, settings files), the operator is the trust source, not the egress validator.

**Carved-out call sites:**

- **KaraKeep asset / bookmark fetches** — `aizk.conversion.utilities.fetch_helpers.fetch_karakeep_asset` and `aizk.conversion.utilities.bookmark_utils.fetch_karakeep_bookmark` use `KarakeepClient` directly with the operator-configured `KARAKEEP_BASE_URL`.
  The canonical self-hosted KaraKeep deployment runs on a private network, which the deny-list would reject; this is the paradox that motivated the post-implementation revision (see "Defer egress validation to fetch time only" above).
- **VLM picture-description endpoint** — `aizk.conversion.workers.converter._call_vlm_api` and the `PictureDescriptionApiOptions` constructed in `_get_picture_description_options` issue HTTP against `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL`.
  The endpoint is OpenAI-compatible (OpenRouter, vLLM, Ollama, internal model server, etc.) and is whatever the operator configured.
  Internal vLLM clusters typically run on private addresses; routing through the egress deny-list would refuse the connection.

**Operator responsibility:**

The operator SHALL ensure every value of:

- `KARAKEEP_BASE_URL`
- `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL`

…points at infrastructure they trust.
A misconfigured value pointing at attacker-controlled or accidentally-public infrastructure is an operational concern, not an application-layer one.
This boundary is documented in the operator deployment guide.

**What the carve-outs do and do not bypass:**

- Bypassed: deny-list destination classification, connection pinning, manual redirect loop with per-hop revalidation, body cap.
- NOT bypassed: TLS verification (default `httpx` posture: hostname check on, cert verify on), request/response timeouts, the operator's network-level egress rules (firewall, ingress controller).

**Enforcement check at code review:**

The invariant "every fetch of _user-supplied_ content goes through `egress_fetch_bytes`" is what reviewers should grep for.
The invariant "every outbound HTTP from the conversion process goes through `egress_fetch_bytes`" is **incorrect** as stated; reviewers should rely on the carved-out list above when judging new call sites.
Adding an outbound call against an operator-configured URL is permitted; adding one against a user-supplied URL without `egress_fetch_bytes` is a regression.

**Rationale:**

The egress validator's deny-list is a function of "what does the application's threat model treat as untrusted?"
Operator-configured infrastructure URLs are not in that set.
Forcing them through the deny-list would either block legitimate self-hosted deployments (KaraKeep / vLLM on a private network) or require an operator allowlist surface that the egress design has already rejected (see "Defer egress validation" alternatives).

**Alternatives considered:**

- Add an operator allowlist (`AIZK_EGRESS_ALLOWLIST`) and route VLM and KaraKeep through `egress_fetch_bytes`: rejected — see the "Defer egress validation" decision's alternatives section.
- Add a `bypass_deny_list=True` parameter to `egress_fetch_bytes` so operator-trusted callers get the connection-pin / redirect-loop benefits while skipping the deny-list: deferred.
  Possible future hardening; the current call sites get adequate TLS posture from default `httpx` behavior.

## Architecture

```text
                 inbound JSON (bookmark.source_url, etc.)
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ UrlRef construction     │  normalize URL for dedup identity;
                 │ (pydantic validator)    │  NO DNS, NO destination classification
                 └─────────────────────────┘
                              │ UrlRef (egress not yet validated)
                              ▼
                 ┌─────────────────────────┐
                 │ KarakeepBookmarkResolver│  may build UrlRef for KaraKeep asset URLs
                 │  / ArxivResolver / etc. │  (private base_url is fine — fetcher routes
                 └─────────────────────────┘   those through KarakeepClient, not egress gate)
                              │ UrlRef
                              ▼ ─── trust boundary: fetch time ───
                 ┌─────────────────────────┐         ┌────────────────────────────┐
                 │ UrlFetcher              │ ──────► │ async_assert_egress_allowed│
                 │ ArxivFetcher            │         │  (DNS + classification —   │
                 │ GithubReadmeFetcher     │         │   load-bearing check)      │
                 └─────────────────────────┘         └────────────────────────────┘
                              │ ValidatedDestination (ip, host, scheme)
                              ▼
                 ┌─────────────────────────┐
                 │ Connection-pinned httpx │  per-hop revalidation on 3xx
                 │ transport               │  (max 5 hops; strip auth headers
                 │                         │   cross-host; reject https→http)
                 └─────────────────────────┘
                              │ bytes
                              ▼
                 ┌─────────────────────────┐
                 │ Pre-fetch <img src> URLs│ ──► async_assert_egress_allowed
                 │ rewrite HTML to point   │     fetch into workspace/
                 │ at workspace-local      │     prefetched-images/<sha256>.<ext>
                 │ copies (lxml.html)      │     (streaming cap; content-type ext)
                 └─────────────────────────┘
                              │ HTML with workspace-local refs only
                              ▼
                 ┌─────────────────────────┐
                 │ Docling subprocess      │
                 │ enable_remote_fetch=F   │
                 │ enable_local_fetch=T    │
                 │ workspace-confinement   │ ──► _assert_within(workspace, path)
                 │ gate on local-fetch     │
                 └─────────────────────────┘
                              │ metadata.json (markdown_filename, figure_files)
                              ▼
                 ┌─────────────────────────┐
                 │ Parent uploader         │ ──► _assert_within(workspace, name)
                 │ markdown_path /         │     (subprocess-metadata seam)
                 │ figure_paths            │
                 └─────────────────────────┘
                              │ validated absolute paths
                              ▼
                            S3 upload
```

## Risks

- **TOCTOU between DNS validation and socket connect.**
  Mitigation: connection-pinned transport carries the validated IP forward to the socket layer; DNS does not run inside `httpx`.
  Implementation review must confirm the transport actually uses the captured IP and not a fresh resolution.
- **Hostname resolves to mixed public + private IPs.**
  Mitigation: reject if ANY resolved address is in the deny set (not "any one allowed is enough").
  Test scenario covers this.
- **IPv4-mapped IPv6 smuggling (`::ffff:10.0.0.5`).**
  Mitigation: normalize via `ip_address.ipv4_mapped` before classification.
- **NAT64 / 6to4 IPv6 smuggling (`64:ff9b::169.254.169.254`, `2002:ac10::/32`).**
  Mitigation: explicit prefix checks for `64:ff9b::/96` and `2002::/16` in the deny set.
- **Relative redirect URL.**
  Mitigation: resolve relative `Location` against the previous URL before re-validation; the manual redirect loop handles this explicitly.
- **Pre-fetch amplification.**
  An adversarial HTML with many `<img>` URLs could fan out into many outbound requests or fill disk.
  Mitigation: cap at 50 images, 100 MiB total, 10 MiB per image (streaming-enforced), 60 s phase deadline.
  Out-of-scope for this change is broader per-job or global rate-limiting.
- **`enable_remote_fetch=False` scope** — verified: Docling's HTML backend only calls `_load_image_data` for `<img src>` attributes.
  `<link>`, `<script>`, `<iframe>`, `srcset`, `<picture><source>`, CSS `url()`, and SVG `<image>` are parsed as text or ignored and produce no I/O.
  Setting `enable_remote_fetch=False` fully prevents all outbound network activity; no pre-scrub step is required.
  Covered by `tests/conversion/unit/utilities/test_docling_remote_fetch_coverage.py`.
- **Docling local-fetch containment** — `local_fetch_root` does not exist as a field in `HTMLBackendOptions`; the subclass fallback was taken.
  See Workspace-confinement decision and `src/aizk/conversion/utilities/docling_backend.py`.
- **Symlink-escape inside workspace.**
  Mitigation: `_assert_within` calls `resolve()` (which follows symlinks) before the `is_relative_to` check, so a symlink pointing out of the workspace fails containment.
- **TOCTOU between `_assert_within` check and parent `open()` of subprocess artifact.**
  A malicious subprocess could swap a validated file path for a symlink between the containment check and the subsequent `open()` call.
  Mitigation: callers open subprocess-produced files with `O_NOFOLLOW`; `ELOOP` is caught and re-raised as `WorkspaceEscape`.
  See the Path containment helper decision for the open pattern.
- **TOCTOU on parent-side reads of subprocess-produced `metadata.json`.**
  The parent reads `metadata.json` to obtain `markdown_filename`, `figure_files`, `terminal_ref`, and the config snapshot.
  A compromised converter subprocess could swap that file for a symlink pointing at any host-readable JSON between workspace creation and parent read, feeding tampered values into the manifest, DB row, and S3.
  Mitigation: parent-side reads use `read_text_nofollow()` (`O_RDONLY | O_NOFOLLOW`), so a leaf symlink at open time raises `WorkspaceEscape` rather than being followed.
  Used at both `uploader._upload_converted` and `orchestrator.process_job_supervised`'s post-conversion enrichment read.
- **Latency-based oracle at JSON ingress.**
  _(Revised — no longer applies; see "Defer egress validation to fetch time only" decision.)_ Egress DNS validation no longer runs at `UrlRef` construction.
  The fetch-time check inside `UrlFetcher` / `ArxivFetcher` / `GithubReadmeFetcher` / `prefetch_images` is the trust boundary, executed by the worker — not in API request handling.
- **Future legitimate need to reach a private host (e.g., internal arxiv mirror).**
  Mitigation: out of scope at cutover.
  When that need arises, address by extending the spec — not by adding a runtime allowlist knob today, which would be a security regression for the much more common case of zero-private-host needs.
