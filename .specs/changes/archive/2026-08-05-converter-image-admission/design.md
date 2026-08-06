# Design: Converter Image Admission

## Context

The conversion pipeline hands untrusted HTML — archived web pages from KaraKeep — to Docling.
Docling resolves `<img src>` while parsing and reads whatever the attribute points at, so the pipeline has always needed a control over which references Docling is allowed to resolve.

That control lived in `make_confined_backend(workspace)`: a `HTMLDocumentBackend` subclass that overrode `_load_image_data`, resolved the path, and raised `WorkspaceEscape` when it landed outside the job workspace.
Somewhere between Docling 2.95 and 2.117 — the two versions this project has resolved, so the exact release is not established — image loading moved into `docling.backend.utils.image_resource_loader.ImageResourceLoader`.
In 2.117, `HTMLDocumentBackend._load_image_data` survives as a two-line shim that nothing calls; the live path is `_create_image_ref` → `ImageResourceLoader.create_image_ref`.
`HTMLDocumentBackend._is_remote_url`, which the override called on its first line, no longer exists.
The override is therefore both unreachable and, if reached, broken.

The controls in the new class shape this design:

- `resolve_relative_path(loc, base_path)` resolves a reference against `base_path`.
  When `base_path` is a remote URL it `urljoin`s; when `base_path` is a local path it rejects absolute `loc` outright and confines the result to `base_path`'s parent directory.
- `load_image_data` refuses any local read when `base_path` is unset, and refuses remote reads unless `enable_remote_fetch` is set.
  Remote reads that are allowed are SSRF-validated.
- `base_path` comes from one option, `HTMLBackendOptions.source_uri`, and serves both image resolution and `<a href>` resolution.
- Resolution errors raised by `resolve_relative_path` propagate out of `convert()`; load errors raised inside `create_image_ref` are caught and downgraded to `warnings.warn`.

The pipeline pre-fetches every `<img src>` through the egress-validated helper and rewrites each to an **absolute** workspace path, and it sets `source_uri` to the page's origin URL.
Under 2.117 that combination admits nothing.
In the configuration actually in effect, the absolute local path is `urljoin`ed onto the origin — `/tmp/ws/img.png` becomes `https://origin.example/tmp/ws/img.png` — and is then refused as a remote fetch, because remote fetching is disabled.
With `source_uri` unset instead, the read is refused for a missing `base_path`.
The library's rejection of absolute paths applies only when `base_path` is itself a local path, which this pipeline does not use, so it is not the mechanism failing here.
Measured against the installed library, both configurations yield `pictures=1 with_image=0`.
HTML conversions currently produce no figures, upload no figure blobs, and never run VLM alt-text — and say nothing about it, because the failure is a Python warning.

Two project constraints shape the contract:

- The egress requirement lives in `pluggable-pipeline` and binds any converter, not just Docling.
  Contract prose must sit at the intersection of what any converter can guarantee; Docling-specific behavior belongs here.
- The committed lockfile still resolves Docling 2.95.0.
  The 2.117.0 bump is uncommitted, so no conversion has run against the broken path and there is no backfill to plan.

## Decisions

### Decision: AdmitAtThePrefetchBoundary

**Chosen:** Make the pre-fetch phase the sole admission point.
A reference is admitted only if it passes egress validation and the per-document caps; anything else never reaches the converter in dereferenceable form.

**Rationale:** The control that broke twice was anchored to a Docling internal.
Pre-fetch is code this project owns, in a phase that already performs egress validation and already classifies failures.
Anchoring admission there removes the possibility of a library-internal relocation silently detaching it.
It also puts the decision before the converter rather than inside it, which is what lets the contract be stated converter-neutrally.

**Alternatives considered:**

- Re-anchor the override on `_create_image_ref` or inject a confined `ImageResourceLoader`: keeps an independent, loudly-raising check, but re-couples to a private attribute of the same class that has now moved twice.
  This is the status quo ante and it is what failed.
- Delegate entirely to Docling's own confinement: no code to maintain, but confinement lands on the input file's parent directory rather than a directory we choose, and a violation becomes a `warnings.warn` returning `None` — an attempted traversal turns invisible instead of logged.

### Decision: InlineAdmittedImageBytes

**Chosen:** Rewrite an admitted `<img src>` to a `data:` URI carrying the fetched bytes.
Keep `source_uri` set to the page's origin URL.

**Rationale:** `data:` is the one branch of `load_image_data` that needs neither `base_path` nor a filesystem read, so admitted images load with no local dereference at all.
Keeping `source_uri` on the origin URL preserves `<a href>` resolution, which shares the same option.
Measured against the installed library: admitted image loads (`with_image=1/1`), `href="/about"` resolves to `https://origin.example/about`, and every un-admitted reference shape — absolute path, traversal, remote URL — is neutralized without raising.

**Alternatives considered:**

- Set `source_uri` to a workspace-local path and rewrite srcs relative to it, letting Docling's confinement land on our workspace.
  Measured and rejected: with a local `base_path`, `resolve_relative_path` raises `ValueError` on any absolute `<a href>` (`"Absolute paths are not allowed with local base_path"`), and that error escapes `convert()`.
  `href="/about"` aborts the entire conversion, and real pages carry such links constantly.
  It also loses origin-relative hyperlink resolution, since one option serves both.
- Leave srcs as absolute workspace paths and set `enable_local_fetch=True` with a local `base_path`: same absolute-path rejection, this time on the images themselves.

### Decision: DeriveDataUriMediaTypeFromContentType

**Chosen:** Build the `data:` URI's media type from the fixed Content-Type allowlist `_CONTENT_TYPE_TO_EXT` already uses.
When the response Content-Type is absent or outside the allowlist, emit `image/octet-stream` and let the converter's image decoder sniff the real format from the bytes.

**Rationale:** Docling strips the prefix with `re.sub(r"^data:image/.+;base64,", "", src_loc)`, so any media type outside `image/*` leaves the prefix inside the payload and the base64 decode fails.
It fails silently, too: the resulting `binascii.Error` is a `ValueError`, which `create_image_ref` catches and downgrades to a warning.
Confirmed against the installed library — `application/octet-stream` is not stripped, `image/octet-stream` is.
Constraining the media type to `image/*` is therefore what keeps admitted bytes decodable at all.
The `image/octet-stream` fallback preserves current behaviour for images served with a missing or unusual Content-Type, which land as `.bin` on disk today and still render because the decoder sniffs the format from the bytes.
The URL path remains excluded as a type source, as it is today.

**Alternatives considered:**

- Decode each image during pre-fetch to determine its true media type: accurate, but adds a full image decode per image to the fetch loop plus a new failure mode, to produce a label the decoder derives anyway.
- Treat an unknown Content-Type as a non-admission: simpler, but drops images that render correctly today purely because a server omitted a header.

### Decision: AlignConverterBase64CeilingWithPrefetchCap

**Chosen:** Set `HTMLBackendOptions.max_image_data_base64_bytes` explicitly from the pre-fetch policy's `per_image_max_bytes` rather than leaving it at the library default.

**Rationale:** Both values cap the byte count of a single image, and the library default of 20 MiB is larger than the pipeline cap of 10 MiB.
Left implicit, an operator raising `AIZK_PREFETCH_PER_IMAGE_MAX_BYTES` above 20 MiB would pass the pipeline's own cap and then be silently rejected by the converter, with the rejection surfacing only as a warning.
Deriving the converter's ceiling from the pipeline's cap makes the pre-fetch policy the single source of truth for per-image size and removes the drift entirely, rather than documenting it as a hazard to remember.

**Intended side effect:** lowering the ceiling from 20 MiB to the pipeline's 10 MiB also newly rejects `data:` images the page itself authored at between 10 and 20 MiB, which convert successfully today.
That is the point rather than a regression.
A `data:` reference is the one class admitted without a fetch, so it is also the one class that currently bypasses the per-image cap entirely: a page can inline a 19 MiB image and evade a limit that applies to every fetched image.
Deriving the ceiling closes that bypass, so the per-image cap means the same thing however the bytes arrived.

**Alternatives considered:**

- Leave the library default and note the coupling in `Risks`: costs nothing to implement, but leaves a silent failure mode armed behind an operator-facing knob.
- Raise the pipeline's `per_image_max_bytes` to the library's 20 MiB default: changes an operator-facing cap value, which this change holds out of scope, and inverts the ownership — the pipeline's policy should govern the converter's limits, not the reverse.

### Decision: StripUnadmittedReferences

**Chosen:** On any non-admission — egress rejection, per-image size cap, per-host cap, count cap, phase deadline, workspace-write failure, network error, unparsable reference, or unexpected error — delete the `src` attribute rather than leaving the original value in place.

**Rationale:** Today a failed pre-fetch leaves the original `src` and relies on the converter to refuse it.
That leaves the refusal invisible: the reference silently fails inside the converter and nothing on our side records that an image was lost.
With the attribute gone, Docling takes its `not src_loc` branch, adds a figure placeholder, and performs no I/O.
The refusal is recorded once, by us, with the failure class — which is the loud typed signal that deleting `WorkspaceEscape` would otherwise cost us.

**Alternatives considered:**

- Remove the whole `<img>` element: loses the placeholder, so the converted document gives no indication an image was there.
- Leave the `src` and rely on the converter's capability removal alone: equally safe, but it forfeits the record of what was dropped, which is the observable half of the contract.

### Decision: ResolveReferencesAgainstSourceUrl

**Chosen:** Resolve each `<img src>` against the source URL with `urllib.parse.urljoin` inside the existing pre-fetch loop.
`prefetch_images` takes a new keyword-only `source_url` parameter; when it is `None`, references are evaluated as-is.

**Ordering:** resolution happens **before the per-host cap keys the reference**, not merely before egress validation.
The per-host bucket is keyed on `urlparse(src).hostname or ""`.
If resolution ran after that keying, every page-relative reference would fall into the single empty-host bucket, and the eleventh relative image on a page would be dropped as a per-host violation — silently regressing the exact story that motivates resolving at all.

**Reference shapes excluded before resolution:** a `src` that is empty, whitespace-only, or fragment-only (`#…`) is skipped rather than resolved.
`urljoin("https://origin.example/a/b/page.html", "#frag")` returns the page's own URL, so resolving a fragment-only reference would issue a live fetch of the source page and then inline its HTML body as image bytes — a request that does nothing useful and a decode failure that carries no useful class.

**The `data:` scheme is normalised, not merely detected.** `DATA:image/png;base64,…` is valid in a browser, but the converter matches the scheme case-sensitively.
Detecting mixed case and passing the attribute through verbatim is worse than not handling it at all: the reference bypasses admission and is then refused inside the converter with no record and no summary count, where an unhandled `DATA:` would at least have resolved, failed the egress scheme check, and been recorded.
Lower-casing the scheme on pass-through is what makes the admission actually hold.

**Parse failures drop one reference, never the document.** `urljoin` and `urlparse` both parse an attacker-controlled string and raise `ValueError` on a malformed authority — an unbalanced IPv6 bracket (`http://[::1/x.png`) or an over-long IPv6 literal.
Letting that escape would turn a single hostile attribute into a failed job, and because the wrapper classifies the failure as retryable and copies its message into the persisted error, it would also feed page-controlled text into the operator-facing error and retry it forever.
Resolution and host keying therefore sit inside one guard that routes a parse failure to the same non-admission path as every other arm.

**Rationale:** Page-relative references (`src="images/photo.png"`) are not fetchable as written, so today they fail egress validation and are skipped — every relative image on every page is silently lost.
Resolving first turns them into ordinary outbound URLs the egress policy can evaluate, which is what the requirement already says should happen to them.
Non-`http(s)` schemes that survive resolution unchanged (`javascript:`, `file:`) are rejected by the egress policy's scheme rule, so resolution widens what is evaluated without widening what is allowed.

**Alternatives considered:**

- `lxml.html.HtmlElement.make_links_absolute(source_url)` over the whole document: one call, but it also rewrites `<a href>`, mutating content that Docling already resolves correctly from `source_uri`.
  Per-`src` `urljoin` touches only what this change requires.

### Decision: RecordEveryNonAdmissionIndividually

**Chosen:** Emit a per-reference record for every non-admission, including the four cap arms, and account for cap-dropped references in the end-of-phase summary.

**Rationale:** The pre-fetch phase records the four exception arms — egress rejection, oversize, disk error, unexpected error — per reference today.
The four cap arms do not: count, total-bytes, deadline, and per-host each log once per document behind a `cap_hit_logged` or `host_cap_logged` latch and identify no reference at all.
The summary compounds it, computing `skipped` as the sum of the four exception counters only, so a document that lost forty images to the count cap reports `skipped=0`.
That is the `no-silent-content-loss` story failing on its headline case: an operator cannot distinguish a page with no images from a page whose images were all dropped, which is the exact confusion the story exists to remove.
The latch stays for the human-readable "cap reached" warning, since one of those per document is enough; what it must stop suppressing is the per-reference record and the counter.

**Alternatives considered:**

- Weaken the contract to per-class recording and leave the code alone: cheaper, but it keeps the count of lost images unobservable, which is the part an operator actually needs.
- Drop the latch entirely and log a warning per reference: restores the information at the cost of fifty warning lines for one capped document; a per-reference record at debug level plus an accurate summary carries the same information without the noise.

### Decision: RemoveConverterDereferenceCapabilityRatherThanEnumerateShapes

**Chosen:** Configure the converter so it can dereference nothing at all — `enable_local_fetch=False`, `enable_remote_fetch=False`, `render_page=False` — and treat that configuration as a stated, load-bearing part of the contract rather than as an unstated backstop.
Strip only `<img src>`, which the admission step must rewrite anyway.
Delete `src/aizk/conversion/utilities/docling_backend.py` and drop the `workspace` parameter from `_create_document_converter`.

**Rationale:** Admission rewrites `<img src>` because that is where the bytes go.
It does not touch other resource-bearing shapes — `<source srcset>`, `<object data>`, `<embed src>`, SVG `href`, CSS `url()`, `poster`, the legacy `background` attribute — which survive into the document the converter parses.
Enumerating and stripping all of them would be a deny-list over an open-ended and growing set of HTML attributes; missing one produces false confidence that enforcement is happening at our boundary when it is not.
Removing the capability instead is complete by construction: a shape nobody enumerated is covered, because the claim is about what the converter can do rather than about which attributes exist.
The cost is that the property now rests on few levers, so the levers are named here, asserted by test, and revisited on every version bump.

Three facts about the current library make this safe, and all three are upgrade-recheck obligations tied to `BoundDoclingVersionRange`:

1. Docling's HTML backend performs I/O for exactly one reference shape.
   Its only resolution sites are `_create_image_ref` for `<img src>`, and `_use_hyperlink` for `<a href>`, which resolves a string and opens nothing.
   No handling exists for `srcset`, `<picture><source>`, `<object>`, `<embed>`, SVG resources, or CSS `url()`.
2. `enable_remote_services`, which the pipeline does set when a picture-description endpoint is configured, governs model inference engines only and never resource fetching.
3. `render_page` is a third lever, and it is not covered by the other two.
   With it enabled, Docling drives a headless browser whose request filter allows the `file`, `data`, `about`, and `blob` schemes outright and consults `enable_remote_fetch` only for remote URLs — so a browser-rendered page could read local files regardless of `enable_local_fetch=False`.
   It defaults to false and the pipeline does not set it; the paired test asserts that it stays false.

The standing evidence for fact 1 is the existing multi-shape coverage test, which feeds a document containing `<link>`, `<script>`, `<img srcset>`, `<picture><source>`, `<iframe>`, CSS `url()`, and SVG `<image>` and asserts zero outbound calls.
It must keep passing on every upgrade; it is the check that would catch Docling growing a new dereference site.

**Alternatives considered:**

- Widen stripping to every resource-bearing attribute: gives two independent layers for each shape enumerated, but only for shapes enumerated correctly.
  It is a deny-list against an open set, it would not have caught the `render_page` path at all, and it grows with the HTML spec.
- Leave `enable_local_fetch=True` and rely on stripping: no benefit once nothing references a path, and it leaves a reachable read path for any shape the admission step does not recognize.

### Decision: KeepWorkspaceCopyOfFetchedBytes

**Chosen:** Continue writing each fetched image to `workspace/prefetched-images/<sha256>.<ext>`.

**Rationale:** Minimal scope — the write already exists and removing it is not required by the contract.
It remains useful for inspecting a live job on the host.

**Note:** the copy becomes write-only; nothing reads it back once bytes are carried inline, and the workspace is a per-job temporary directory discarded at the end.
Dropping the write is a reasonable follow-up if the disk cost matters on a minimal-infrastructure host, but it is out of scope here.

### Decision: BoundDoclingVersionRange

**Chosen:** Declare `docling[easyocr,vlm]>=2.117.0,<2.118.0` in `pyproject.toml`.

**Rationale:** An operator preference for deliberate upgrades, requested alongside this change.
The drift that motivated it was a minor-version move (2.95 → 2.117); an upper bound at the next minor blocks that class without pinning to a single patch.
`uv.lock` continues to pin the exact resolved version.

**Note:** this asserts nothing about system behavior and serves no requirement in the delta spec.
It is a dependency declaration carried by this change, not a contract this codebase makes.

**Alternatives considered:**

- `==2.117.0`: blocks patch-level fixes too, with no drift benefit over `<2.118.0` since the lockfile already pins exactly.
- Bound the transitive `docling-core` / `docling-parse` / `docling-ibm-models` constraints as well: the lockfile pins them exactly and Docling's own constraints govern their range; bounding them here would fight resolution for no gain.

## Architecture

```text
  fetched HTML (untrusted)
          |
          v
  +-------------------------------------------------------------+
  |  prefetch_images(html, workspace, source_url=..)             |
  |  == THE ADMISSION BOUNDARY ==                                |
  |                                                              |
  |  for each <img src>:                                         |
  |    data: URI ........................ admit unchanged        |
  |    otherwise:                                                |
  |      urljoin(source_url, src)                                |
  |      egress_fetch_bytes  -- egress policy, redirects, caps   |
  |        ok   -> write workspace/prefetched-images/<sha>.<ext> |
  |                set src = "data:<mime>;base64,<bytes>"        |
  |        fail -> del src   + log(failure class)                |
  +-------------------------------------------------------------+
          |
          |  <img src>: only data: URIs and attribute-less <img>
          |  every other reference shape: passes through untouched
          v
  +-------------------------------------------------------------+
  |  DocumentConverter (Docling)                                 |
  |    enable_remote_fetch = False  \                            |
  |    enable_local_fetch  = False   > capability removal        |
  |    render_page         = False  /                            |
  |    source_uri          = origin URL  (for <a href> only)     |
  |                                                              |
  |  ImageResourceLoader.load_image_data:                        |
  |    data: branch  -> decode, no base_path, no filesystem      |
  |    no src        -> placeholder picture, no I/O              |
  +-------------------------------------------------------------+
          |
          v
  DoclingDocument -> _extract_figures -> figure blobs -> S3
```

The two boxes carry different parts of the contract, and neither covers the whole of it.

The first box is the **admission** boundary: it decides which references become bytes, and it is what makes the outcome observable, because it is where every non-admission gets recorded.
It reaches only `<img src>`, which is the shape it must rewrite anyway.

The second box is the **capability** boundary: its three flags are what make every other reference shape — `<source srcset>`, `<object data>`, CSS `url()`, and any shape not yet invented — unresolvable.
For those shapes the flags are not defence in depth; they are the only layer, which is why they are stated in the contract rather than treated as an implementation detail.
`<img src>` is the one shape with two independent layers.

## Risks

- **Base64 inflation of the in-memory document.**
  Admitted bytes are carried in the HTML string at roughly 4/3 size, and several copies are live at once near the peak.
  At the current caps (50 images, 100 MiB total) that is up to 100 MiB written to the workspace, a ~133 MiB unicode string from lxml serialization, its UTF-8 encoding into a second buffer before the converter is called, and the parse tree the converter builds over that buffer.
  Mitigation: caps are unchanged and realistic archived pages are orders of magnitude smaller; the total-bytes cap is the knob if it ever bites, and dropping the workspace write (see `KeepWorkspaceCopyOfFetchedBytes`) removes one term.
  Recorded rather than pre-optimized, because the peak is bounded by an operator-set cap rather than by page content.
- **Converter and pipeline per-image caps could drift.**
  Closed by `AlignConverterBase64CeilingWithPrefetchCap` rather than mitigated: the converter's ceiling is derived from the pre-fetch policy, so the two cannot disagree.
  The residual risk is that a future backend option is added with its own independent default; a paired test asserts the derived value rather than a literal.
- **Loss of a typed, raising failure for traversal attempts.** `WorkspaceEscape` no longer fires for image references.
  Mitigation: the contract requires every non-admission to be recorded individually with its failure class.
  Pre-fetch does this today for the four exception arms only, which is why `RecordEveryNonAdmissionIndividually` extends it to the cap arms and the phase summary.
  The paired test asserts the specific non-admission class, not merely that some record exists — a weaker assertion would also pass if the reference had simply failed to resolve.
- **Non-admission classes must name the actual cause.** `socket.gaierror` and `ConnectionError` are `OSError` subclasses, so a single `except OSError` arm reports a name-resolution failure to operators as a disk problem.
  That is tolerable for a counter and not for a class whose whole purpose is triage.
  Mitigation: the workspace write raises a distinct error, so disk failures and network failures land in separate arms with separate classes.
- **Relative references become fetchable where they previously were not.**
  Resolving against the source URL means pages with relative images now issue outbound requests that today are skipped.
  Mitigation: those requests go through the same egress validation and the same per-host and per-document caps as any other; the change makes previously-lost images available rather than widening policy.
- **SVG images are admitted but not rendered.**
  An SVG passes egress validation and is inlined like any other image, but the converter's image decoder cannot open it, so the figure drops with a converter-side warning rather than a recorded non-admission.
  This matches current behaviour, where Docling skips `.svg` paths outright.
  Mitigation: accepted rather than fixed.
  Encoding "which formats the converter can render" into the admission boundary would rebuild exactly the converter coupling this change removes.
- **Figure placeholders where images were dropped.**
  A stripped `src` yields a `PictureItem` with no image.
  `_extract_figures` already skips these (`get_image` returns `None`), and Docling already produced the same shape whenever an image failed to load, so serialized output is unchanged in kind.
  Covered by a paired test rather than assumed.
