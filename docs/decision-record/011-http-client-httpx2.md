# 011 - HTTP Client: httpx2

## Status

September 25, 2026 - Accepted

## Context

`aizk` makes its outbound HTTP calls with `httpx`: the conversion fetchers and the egress-pinned transport, the arXiv and GitHub README fetches, the Docling picture-description call, the startup probes, the health route, and the backfill client.
`httpx` 0.28.1 was released in December 2024 and is still the newest release; the lock refresh in September 2026 resolved no later version.
The fetch path handles untrusted URLs, so the HTTP client is a security boundary that needs timely fixes.

Pydantic maintains `httpx2`, a fork of `httpx` 0.28.1.
At the fork only names changed: the package (`import httpx2`), the connection library (`httpcore` became `httpcore2`), the logger name, and the User-Agent (`python-httpx2/<version>`).
Behavior changes since the fork that reach `aizk`:

- From 2.3.0, a client with default verification checks certificates against the operating system's trust store (`truststore`) instead of a bundled CA list.
- From 2.4.0, at most five chained `Content-Encoding` decoders are applied, and `httpx2` deprecation warnings are shown by default.

`httpx2` classes are separate from `httpx` classes: `httpx2.HTTPError` is not a subclass of `httpx.HTTPError`, and an `httpx2.AsyncClient` is not an `httpx.AsyncClient`.

`anthropic`, `fastmcp`, the OpenAI SDK, `pydantic-ai`, and Starlette's `TestClient` already use `httpx2`, and `karakeep-client` 0.5.0 moved to it (its only API change).
Twelve locked packages still depend on `httpx`, among them Docling, `huggingface-hub`, and `litellm`, so both libraries are installed either way.

## Decision

### Selected Approach

`aizk` depends on `httpx2>=2.13.0` in place of `httpx`, and on `karakeep-client>=0.5.0`.
Every client, transport, request, response, and exception handler in `src/aizk` moves to `httpx2` in one commit.
No `aizk` call site hands an `httpx` object to a library, so none keeps `httpx`.
The move lands in the `stage-kit-and-layout` change as its own commit.

### Rationale

- `httpx2` is maintained and its API is the one `aizk` already uses, so the migration is a rename with no logic change.
- Moving every handler in one commit avoids the failure a partial move would cause: an `except httpx.HTTPError` around an `httpx2` call catches nothing.
- `karakeep-client` 0.5.0 raises and catches `httpx2` exceptions and builds an `httpx2` client, so the test double that patches its client construction must build an `httpx2` client too.

### Consequences

#### Positive Impacts

- Security and bug fixes for the fetch path come from a maintained upstream.
- `aizk` and `karakeep-client` use one client library.

#### Potential Risks

- Default-verification clients (the probes, the README fetch, the picture-description call, backfill) now trust the operating system's store; a host without a CA bundle fails every TLS call.
- The User-Agent changes to `python-httpx2/<version>`, which KaraKeep, arXiv, GitHub, and the picture-description endpoint see.
- Both libraries stay installed, because other locked packages depend on `httpx`.

#### Mitigation Strategies

- The container's runtime image (`python:3.12-slim-trixie`) installs `ca-certificates`, and `containers/Containerfile` does not remove it.
- `EgressPinnedTransport` builds its own SSL context (TLS 1.2 minimum, verification required), so the pinned egress path does not depend on the new default.
- The egress transport tests assert the pinned IP and the `sni_hostname` extension, which `httpcore2` reads the same way `httpcore` did.

### Alternative Considered

#### Option 1: Stay on httpx

No code change, but fixes to the fetch path depend on an upstream with no release since December 2024.
Not selected.

#### Option 2: Alias httpx to httpx2 process-wide

`httpx2.alias_httpx()` makes every `import httpx` in the process resolve to `httpx2`.
It would also redirect the twelve other packages that depend on `httpx`, which is a larger change than moving `aizk`'s own call sites, and it must run before any import of `httpx`.
Not selected.

#### Option 3: Move in a later change

Keeps the stage refactor's release free of an HTTP-client change.
Not selected by the owner: the move is a rename, and it lands as its own commit inside the change.

## Implementation Details

- `pyproject.toml`: `httpx2>=2.13.0`, `karakeep-client>=0.5.0`; relock.
- Every `httpx` import and handler in `src/aizk`, `tests`, `notebooks`, and `scripts` moves to `httpx2`; no call site keeps `httpx`.

## Related ADRs

- 001 - Content Collection & Archiving (the fetch path)
- 010 - Karakeep Webhook Integration (the KaraKeep client)

## Additional Notes

- `httpx2` changelog: <https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md>
- `karakeep-client` 0.5.0: <https://github.com/ahgraber/karakeep_client> (tag `v0.5.0`)
