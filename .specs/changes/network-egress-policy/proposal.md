# Proposal: network-egress-policy

## Intent

External content reaches network and filesystem sinks in the worker without validation.
Bookmark `source_url` values flow into `UrlFetcher` and trigger arbitrary outbound GETs; attacker-controlled HTML reaches Docling's HTML backend with `enable_remote_fetch=True` and `enable_local_fetch=True`; subprocess-supplied filenames are consumed by the parent uploader without containment checks.
The Pluggable Pipeline spec currently defines fetcher and converter contracts but is silent on what destinations they may dereference.
This change makes the egress contract explicit and tightens the worker's subprocess→parent trust seam, closing the SSRF/LFI/path-traversal primitives identified in the 2026-04-25 security audit (Vulns 1, 2, 3).

## Scope

**In scope:**

- New "Network egress policy" requirement in `pluggable-pipeline` covering fetchers AND converters: any pipeline component that dereferences a URL or filesystem path derived from external content SHALL apply the same validation.
- Converter-side scenarios for the policy: outbound HTTP requests issued during HTML conversion and local filesystem reads triggered by attacker-controlled HTML must both pass the validation gate.
- New "Validate subprocess artifact metadata" requirement in `conversion-worker` covering the parent-uploader trust seam: filenames produced by the conversion subprocess SHALL be containment-checked against the job workspace before being read or uploaded.
- Extension of `UrlRef` validation contract in `pluggable-pipeline` to make egress validity (not just syntactic validity) part of the model construction contract — so resolvers cannot construct unsafe `UrlRef` values.

**Out of scope:**

- Inbound authentication or trust-model changes — tracked separately under change `deployment-trust-model`.
- Subprocess sandboxing (network namespaces, chroot, UID drop, seccomp) — defense-in-depth, separate future change.
- Widening `_API_SUBMITTABLE_KINDS` or expanding the publicly submittable source kinds.
- Alembic migrations or DB schema changes.
- Any changes to the OpenAPI surface; no new endpoints, no new request/response fields.

## Approach

Mechanism notes parked here for later promotion to `design.md`:

- A shared egress-validation helper in `aizk/conversion/utilities/` (probably `egress.py`, distinct from the syntactic `aizk/utilities/url_utils.py` helpers — those validate URL shape, not destination class).
  The helper resolves the hostname, classifies each resolved address against deny ranges (loopback, RFC1918, RFC6598, link-local, IPv6 ULA + link-local, multicast, `0.0.0.0/8`, `169.254.169.254` / `fd00:ec2::254` cloud-metadata), and returns either an approved IP for connection-pinning or a typed rejection error.
- For redirect handling: replace `httpx`'s `follow_redirects=True` with a manual loop that re-runs the egress validation at every hop and respects a configurable max-redirects cap.
- For Docling: pre-fetch `<img>` URLs via the validated egress helper into a per-job scoped temp directory (already exists as the workspace), rewrite `<img src>` to point at the scoped copies, then run Docling with `enable_remote_fetch=False` and `enable_local_fetch=True` confined to the workspace via `Image.open` against absolute paths inside that dir.
  Net effect: image embedding still works, but every fetch passes the egress gate and no out-of-workspace local read is possible.
- For path containment: tighten `markdown_path` / `figure_paths` in `aizk/conversion/utilities/paths.py` to compose the path, call `resolve()`, and assert `is_relative_to(workspace.resolve())`.
  Reject names containing `/`, `\`, `..`, or that are absolute.
  The same helper SHALL be reused at any future subprocess-metadata seam.
- Egress policy is **deny-list-only** with a fixed deny set defined by the spec.
  No configurable allowlist at cutover.
  The conversion worker has no legitimate need to reach private/internal hosts in any supported deployment, so the deny-list is non-negotiable; if a future deployment does need an exception (e.g., an internal arxiv mirror), that will be addressed by a follow-up spec change rather than a runtime knob.

The spec captures the policy and its observable behavior; the helper internals (which IP-classification library, exact redirect-loop shape, scoped-temp-dir layout) are formalized in `design.md`.

## Schema Impact

No OpenAPI changes expected.
The change is internal to fetcher and converter behavior; no new endpoints, no new request/response shapes, no error-response schema changes.
A before-snapshot will be captured for verification, and the expected-changes file will declare zero diff.

## Open Questions

None remaining at proposal time.
Resolved during proposal review:

- `UrlRef` egress validation runs at **model construction time** — fails fast at JSON ingress, consistent with today's syntactic validation pattern.
- Egress policy is **deny-list-only** with a fixed deny set defined by the spec; no configurable allowlist at cutover.
