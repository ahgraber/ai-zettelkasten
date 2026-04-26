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

**Rationale:** Pydantic validators run synchronously, so `UrlRef` construction needs a sync API.
Async fetchers benefit from the same logic without blocking the event loop on DNS.
The 2-second DNS deadline prevents slow-resolver DoS at ingress.

**Alternatives considered:**

- Async-only with sync-from-async wrapper: rejected; `UrlRef` is constructed inside synchronous pydantic validation, where running an event loop is awkward.

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
The mechanism is determined by a mandatory implementation-time spike:

1. **Spike:** before writing converter code, test whether `HTMLBackendOptions.local_fetch_root` enforces path containment by attempting to open `workspace / "../../etc/passwd"` via Docling's local-fetch path with `local_fetch_root=workspace`.
   If the attempt raises or returns empty, the option enforces containment — use it and add a regression test proving escape is blocked.

2. **Fallback (if `local_fetch_root` does not enforce containment):** subclass `HTMLDocumentBackend` and override the method that resolves local paths (likely `_load_image_data` or its call site).
   The override calls `_assert_within(workspace, path)` before delegating to `super()`.
   Pass the subclass to the document converter via `backend_class=<subclass>` — a supported extension point that avoids patching internals.
   The spike outcome (which branch was taken) MUST be recorded as a comment in the implementation.

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

**`model_construct` bypass note:** the construction-time egress check in `UrlRef.__init__` / `model_validate` runs only when the model is constructed via the normal pydantic path.
`UrlRef.model_construct(...)` bypasses validators.
The fetch-time call to `async_assert_egress_allowed` inside `UrlFetcher` / `ArxivFetcher` is the load-bearing security check; the construction-time check is a fail-fast convenience, not the trust boundary.
No internal code path should call `model_construct` to produce a `UrlRef`.

**Rationale:** Aligns with the existing typed-error pattern (`FetcherNotRegistered`, `ChainNotTerminated`, `FetcherDepthExceeded`).
Non-retryable classification matches the `NoConverterForFormat` precedent for non-recoverable input-shape errors.

## Architecture

```text
                 inbound JSON (bookmark.source_url, etc.)
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ UrlRef construction     │
                 │ (pydantic validator)    │ ──► assert_egress_allowed (sync, 2s deadline)
                 └─────────────────────────┘
                              │ (only if validated; otherwise raise ValidationError)
                              ▼
                 ┌─────────────────────────┐
                 │ KarakeepBookmarkResolver│
                 │  / ArxivResolver / etc. │
                 └─────────────────────────┘
                              │ UrlRef
                              ▼
                 ┌─────────────────────────┐         ┌────────────────────────────┐
                 │ UrlFetcher              │ ──────► │ async_assert_egress_allowed│
                 │ ArxivFetcher            │         │  (DNS + classification)    │
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
- **`enable_remote_fetch=False` scope unverified.**
  Docling's `enable_remote_fetch=False` must gate ALL resource types (link, script, srcset, CSS fonts, SVG image), not just `<img src>`.
  Mitigation: implementation-blocking verification test that exercises each tag type with `enable_remote_fetch=False` and confirms no outbound request fires.
  If any type is unblocked, scrub it from HTML before Docling ingestion.
- **Docling local-fetch containment via `local_fetch_root` may not behave as containment.**
  Mitigation: mandatory implementation-time spike per the Workspace-confinement decision; fallback is subclassing `HTMLDocumentBackend` as specified there.
- **Symlink-escape inside workspace.**
  Mitigation: `_assert_within` calls `resolve()` (which follows symlinks) before the `is_relative_to` check, so a symlink pointing out of the workspace fails containment.
- **TOCTOU between `_assert_within` check and parent `open()` of subprocess artifact.**
  A malicious subprocess could swap a validated file path for a symlink between the containment check and the subsequent `open()` call.
  Mitigation: callers open subprocess-produced files with `O_NOFOLLOW`; `ELOOP` is caught and re-raised as `WorkspaceEscape`.
  See the Path containment helper decision for the open pattern.
- **Latency-based oracle at JSON ingress.**
  DNS resolution at `UrlRef` construction time creates a timing side-channel: requests for internal hostnames may resolve (or fail to resolve) at measurably different speeds than public ones, leaking information about internal name space.
  Mitigation: bounded by the 2-second DNS deadline, which caps the oracle window.
  Full elimination would require constant-time rejection, which is out of scope.
- **Future legitimate need to reach a private host (e.g., internal arxiv mirror).**
  Mitigation: out of scope at cutover.
  When that need arises, address by extending the spec — not by adding a runtime allowlist knob today, which would be a security regression for the much more common case of zero-private-host needs.
