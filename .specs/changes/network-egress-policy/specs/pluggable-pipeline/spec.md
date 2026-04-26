# Delta for pluggable-pipeline

## ADDED Requirements

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
- **THEN** the read is permitted because the resolved path is contained within the workspace directory

## MODIFIED Requirements

### Requirement: Represent content sources as a discriminated union

The existing requirement is extended with one additional construction-time validation clause for `UrlRef`:

`UrlRef` construction SHALL additionally reject any URL whose resolved host falls into the egress deny set defined in "Apply network egress policy to all external-content dereferences"; the rejection SHALL surface as a pydantic validation error at model construction time, not deferred to fetcher dispatch.
This guarantees that a `UrlRef` instance cannot exist in the system carrying an unsafe destination, including refs constructed inside the resolver chain (e.g., a `KarakeepBookmarkResolver` that derives a `UrlRef` from bookmark `source_url` JSON).

(Previously: `UrlRef` construction validated URL shape only — regex, `pydantic.HttpUrl`, and the syntactic normalizer; egress-class destinations were not rejected at the model layer, so a resolver could construct a `UrlRef` carrying `http://169.254.169.254/...` and the unsafe destination would only be caught — or not — at fetcher dispatch.)

#### Scenario: UrlRef construction rejects a deny-set destination

- **GIVEN** a JSON value `{"kind": "url", "url": "http://169.254.169.254/latest/meta-data/"}`
- **WHEN** `UrlRef` is constructed via pydantic deserialization
- **THEN** a `ValidationError` is raised at construction time, before any fetcher or resolver receives the ref

#### Scenario: Resolver cannot emit an unsafe UrlRef

- **GIVEN** `KarakeepBookmarkResolver` processes a bookmark whose `source_url` is `http://10.0.0.5/admin`
- **WHEN** the resolver attempts to construct `UrlRef(url=source_url)` per its Step 5 handling
- **THEN** the `UrlRef` construction raises `ValidationError`, the resolver propagates a typed error, and no downstream fetcher receives a deny-set destination
