# Delta for pluggable-pipeline

## MODIFIED Requirements

### Requirement: Apply network egress policy to all external-content dereferences

Any pipeline component (fetcher, ref resolver, converter) that dereferences a URL or local filesystem path derived from external content SHALL apply the egress policy defined below before issuing the request or read.
A "dereference of external content" is any operation that resolves a URL to its bytes, opens a filesystem path, or causes another component to do so on its behalf (e.g., a converter passing fetched HTML into a backend that itself follows `<img src>` references).

The egress policy is **deny-list-only** with a fixed deny set:

- IPv4: loopback (`127.0.0.0/8`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), shared-address space (`100.64.0.0/10`, RFC 6598), link-local (`169.254.0.0/16`, including the cloud-metadata address `169.254.169.254`), unspecified-source (`0.0.0.0/8`), broadcast (`255.255.255.255`), and IPv4 multicast (`224.0.0.0/4`).
- IPv6: loopback (`::1/128`), unspecified (`::/128`), unique local (`fc00::/7`), link-local (`fe80::/10`), multicast (`ff00::/8`), and the AWS IPv6 metadata address `fd00:ec2::254`.
- Schemes: any scheme outside `https` and `http`.

Network dereferences SHALL:

- Resolve the destination hostname to one or more IP addresses, reject the request if any resolved address is in the deny set, and connect to a validated address with the original Host header preserved (so DNS-rebinding cannot swap the connected IP after validation).
- Re-apply the policy at every redirect hop; redirect chains SHALL NOT cross a hop whose target fails validation, regardless of how many hops preceded.
- Reject schemes outside `https` and `http`.

Local filesystem dereferences triggered during conversion SHALL be confined to the job's per-conversion workspace directory; any attempt to read a path outside that workspace SHALL be rejected.

Rejections SHALL surface as a typed error and SHALL be classified as non-retryable; the job SHALL fail with an error message identifying the policy violation class (deny-list category, scheme, redirect hop, or out-of-workspace read) without echoing the rejected destination back into structured output served to clients.

**Operator-trusted KaraKeep carve-out — parsed-origin exact match, never string prefix.**
A candidate URL qualifies for the KaraKeep trusted-infrastructure path only when its effective origin exactly matches the configured `karakeep_base_url` origin (`scheme`, normalized hostname, and effective port) **and** its path begins with `/api/v1/assets/`.
URLs that merely share a textual prefix with `karakeep_base_url` SHALL NOT qualify.
Same-origin URLs outside the asset path prefix SHALL NOT qualify.
Non-qualifying URLs SHALL be treated as ordinary outbound URLs and SHALL go through the normal egress-validation path.

**Trust boundary — fetch time, not construction time.**
`UrlRef` construction performs URL normalization for dedup identity but does **not** call the egress validator and does **not** resolve DNS.
The load-bearing trust boundary is fetch time: every fetcher (`UrlFetcher`, `ArxivFetcher`, `GithubReadmeFetcher`) and the image-prefetch path route through `egress_fetch_bytes`, which calls the async egress validator for the initial hop and again for every redirect target.
A `UrlRef` instance therefore _can_ exist in the system carrying a deny-set URL — the security property is that no outbound socket connection ever opens to a deny-set destination, not that the model rejects the URL at construction.
This shape exists because (1) operator-configured KaraKeep base URLs are typically on a private network and asset fetches that target them are routed through `KarakeepClient` (operator-trusted infrastructure, intentionally exempt from the egress gate), and (2) construction-time DNS would otherwise run synchronously in API request handling, in worker rehydration paths, and inside every resolver-built `UrlRef` — duplicating DNS load and creating a queryable resolution-latency oracle on the public submission endpoint.

#### Scenario: Cloud-metadata IPv4 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `169.254.169.254`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued, the job is classified as a non-retryable failure, and no metadata-service response reaches the converter

#### Scenario: Cloud-metadata IPv6 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `fd00:ec2::254`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued

#### Scenario: RFC1918 destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to an address in `10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any network request is issued

#### Scenario: Loopback destination rejected

- **GIVEN** a fetcher is invoked on a `SourceRef` whose target host resolves to `127.0.0.1` or `::1`
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised

#### Scenario: Public host redirecting to private IP rejected at the redirect hop

- **GIVEN** a fetcher is invoked on a `SourceRef` whose initial host resolves to a public address but the response is a 3xx redirect to a URL whose host resolves into the deny set
- **WHEN** the fetcher follows the redirect
- **THEN** the egress policy is re-evaluated, the redirect hop is rejected with a typed error, and the redirect target is never fetched

#### Scenario: DNS rebinding cannot swap the connected IP after validation

- **GIVEN** a fetcher whose pre-connection DNS resolution returns a public IP for the target host
- **WHEN** the underlying socket connection is established
- **THEN** the connection uses the validated IP captured at validation time (not a fresh resolution), so a same-name later-resolution returning a private IP cannot reach the connection layer

#### Scenario: Non-http(s) scheme rejected

- **GIVEN** a `SourceRef` whose URL uses a scheme other than `http` or `https` (e.g., `file://`, `data:`, `javascript:`, `gopher://`, `ftp://`)
- **WHEN** the fetcher attempts the dereference
- **THEN** a typed egress-policy error is raised before any I/O is performed

#### Scenario: Converter outbound HTTP fetch routed through the egress gate

- **GIVEN** an HTML document containing `<img src="http://169.254.169.254/...">` is handed to the converter
- **WHEN** the converter prepares to embed the image
- **THEN** the image-fetch attempt passes through the same egress validation as a `SourceRef` dereference and the cloud-metadata target is rejected before any network request is issued

#### Scenario: Converter local-file dereference confined to workspace

- **GIVEN** an HTML document containing `<img src="/etc/ssh/ssh_host_rsa_key">` is handed to the converter and the file is not present inside the job's workspace directory
- **WHEN** the converter attempts the dereference
- **THEN** the attempt is rejected by the workspace-confinement gate; no `open()` against `/etc/ssh/...` is performed

#### Scenario: Converter local-file dereference within workspace permitted

- **GIVEN** an image was pre-fetched through the egress gate into the job's workspace and the HTML document references that scoped local copy via an absolute path inside the workspace
- **WHEN** the converter dereferences the local path
- **THEN** the read is permitted because the resolved path is contained within the workspace directory, and the image content is embedded in the converted document

#### Scenario: Prefetched workspace image embedded when the document has a remote origin

- **GIVEN** an HTML document whose origin URL is a remote `https` URL, and an `<img src>` rewritten by the image pre-fetch to an absolute path inside the job's workspace
- **WHEN** the converter processes the document
- **THEN** the image is read from the workspace copy and embedded in the converted document; the path is not resolved against the origin URL and no outbound request is issued for it

#### Scenario: UrlRef construction does not perform egress validation

- **GIVEN** a JSON value `{"kind": "url", "url": "http://169.254.169.254/latest/meta-data/"}`
- **WHEN** `UrlRef` is constructed via pydantic deserialization
- **THEN** construction succeeds (no DNS lookup, no destination classification); the URL is normalized for dedup identity; the deny-set rejection happens at fetch time when the fetcher dispatches

#### Scenario: KaraKeep resolver emits a UrlRef whose URL targets a private host (operator-trusted base URL)

- **GIVEN** `KarakeepBookmarkResolver` processes a bookmark whose Step 3 or Step 4 path constructs `UrlRef(url=f"{karakeep_base_url}/api/v1/assets/...")` and `karakeep_base_url` is on a private network
- **WHEN** the resolver returns the `UrlRef`
- **THEN** construction succeeds; the downstream `UrlFetcher` routes the request through `KarakeepClient` (the configured trusted infrastructure) and does **not** invoke `egress_fetch_bytes`; the egress deny-list is correctly inert for this path

#### Scenario: Deny-set URL rejected at fetch time

- **GIVEN** a `UrlRef(url="http://169.254.169.254/latest/meta-data/")` reaches `UrlFetcher.fetch` (e.g., constructed by a future widening of `_API_SUBMITTABLE_KINDS`)
- **WHEN** the fetcher dispatches via `egress_fetch_bytes`
- **THEN** `async_assert_egress_allowed` raises `DenyListDestination`, the fetcher propagates the typed error unwrapped, the job is classified non-retryable, and no socket connection to the metadata service is opened

#### Scenario: Exact-origin KaraKeep asset URL is trusted

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL qualifies for the carve-out because the origin matches exactly and the path is under `/api/v1/assets/`

#### Scenario: Lookalike host does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal.evil.test/api/v1/assets/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL does not qualify for the carve-out and is processed through the normal egress-validation path

#### Scenario: Same-origin non-asset URL does not qualify for KaraKeep trust

- **GIVEN** `karakeep_base_url = "https://karakeep.example.internal"`
- **AND** a candidate URL of `https://karakeep.example.internal/api/v1/bookmarks/abc123`
- **WHEN** the fetcher decides whether to use the KaraKeep trusted-infrastructure path
- **THEN** the URL does not qualify for the carve-out because the path is outside `/api/v1/assets/`, and it is processed through the normal egress-validation path
