# aizk.conversion

The `aizk.conversion` module fetches external content (a URL, a KaraKeep bookmark, …), converts it to Markdown via Docling in an isolated subprocess, and uploads the result to S3.
This is also where the **durable source identity** the rest of the pipeline is built on is minted — every downstream stage scopes its work by the `source_id` a conversion assigns.

## Pipeline position

Conversion is a stage on the shared [`aizk.pipeline`](../pipeline/README.md) runtime: `ConversionStageHandler` is the domain core, run under `StageRunner` by the `worker` command.
The runner owns the claim loop, bounded concurrency, wall-clock timeout, drain, and stale recovery; the handler owns the unit of work (fetch → convert → upload) and runs it in **subprocess isolation**.

```text
external source  (URL / KaraKeep bookmark / …)
        │  fetch          (outbound egress-gated)
        ▼
  Docling convert          (isolated subprocess)
        │  upload          (subprocess output containment-checked)
        ▼
  S3: markdown + manifest + figures     +     a ConversionOutput row
        │
        ▼
  aizk.chunking → aizk.graph   (downstream, keyed by source_id + conversion_output_id)
```

## What it produces, and the identities it mints

Three rows anchor everything downstream:

- **`Source` (`sources`)** — the canonical, durable identity for anything the system can convert; its `source_id` (a UUID) is the identity every downstream stage scopes on.
  A source is materialized idempotently from the submitted `source_ref` (keyed on `source_ref_hash`), so re-submitting the same reference reuses one `source_id` rather than forking a parallel one.
- **`ConversionJob` (`conversion_jobs`)** — one conversion _attempt_, keyed by an integer `id`, with owner-scoped idempotency (`(owner_id, idempotency_key)` unique), attempt/retry tracking, and a status lifecycle `NEW → QUEUED → RUNNING → UPLOAD_PENDING → {SUCCEEDED, FAILED_RETRYABLE, FAILED_PERM, CANCELLED}`.
- **`ConversionOutput` (`conversion_outputs`)** — the record of a _successful_ conversion, one per job.
  Its integer `id` is the **`conversion_output_id`** downstream stages use as the Markdown _locator_; the row carries the durable `source_id`, the S3 keys (`s3_prefix`, `markdown_key`, `manifest_key`), the content hash (`markdown_hash_xx64`) that lets a later fetch verify it read the exact bytes, and `docling_version` / `payload_version`.

So the two ids the rest of the codebase passes around are born here: `source_id` is the **durable source** (stable across re-conversions), and `conversion_output_id` is the **per-conversion artifact locator** (a new one each successful run).
Work-unit progress is co-committed to the shared `pipeline_events` table keyed by `source_id`, so a source's status is resolvable across every stage — query that projection, not the worker's internals.

## Using it

### CLI

`aizk-conversion` console script (or `python -m aizk.conversion.cli`):

- `aizk-conversion serve` — the JSON API + HTML operator UI over uvicorn (default `0.0.0.0:8000`).
- `aizk-conversion worker` — drives the conversion stage: claims queued jobs and runs fetch → convert → upload under `StageRunner`, in subprocess isolation.
  Runs migrations on startup and refuses to accept work if a startup dependency probe (S3, database, and any configured fetch/description endpoints) fails.
- `aizk-conversion db-init` — run Alembic migrations over the shared tree (the same tree the graph stage reuses).

The service owns SQLite replication via Litestream (started role-gated by `serve` / `worker`); the graph stage reuses this database and does not replicate it.

### HTTP API

- **Jobs** — `POST /v1/jobs` (submit; deduped on `(owner_id, idempotency_key)`; `503` when the queue is at capacity), `GET /v1/jobs` (+ `/{id}`, `/status-counts`), `POST /v1/jobs/{id}/retry`, `POST /v1/jobs/{id}/cancel`, `POST /v1/jobs/actions` (bulk).
  Which source kinds may be submitted is a per-deployment policy (`accepted_submission_kinds`).
- **Outputs** — `GET /v1/outputs/{output_id}/{manifest,markdown,figures/{filename}}` fetch the produced artifacts.
- **Misc** — `GET /v1/bookmarks/{source_id}/outputs`; `GET /health/live`, `GET /health/ready`.
  The service is JSON-only (root redirects to `/docs`); operator job monitoring lives in the console app (`aizk.console`, served by `aizk-graph`).

### Configuration

- `AIZK_*` (`ConversionConfig`) — S3 endpoint/bucket/credentials; queue depth and worker knobs (`WORKER_CONCURRENCY`, `WORKER_JOB_TIMEOUT_SECONDS`, stale/retry); the API listener (`API_HOST` / `API_PORT` / `API_RELOAD`); `TRUSTED_HOSTS`; and the outbound-amplification caps (`PREFETCH_*`, `EGRESS_*`).
- `AIZK_CONVERTER__DOCLING__*` (`DoclingConverterConfig`) — the Docling adapter: page cap, OCR, table structure, and the optional picture-description endpoint.
- `AIZK_FETCHER__KARAKEEP__*` (`KarakeepFetcherConfig`) — the KaraKeep bookmark fetcher endpoint.
- `AIZK_AUTH_MODE` / `AIZK_DEFAULT_PRINCIPAL` (`AuthSettings`) — `auth_mode` is `trust_network` only today (no app-layer auth; internal deployment); `default_principal` supplies the `owner_id` stamped on rows.

## Safety at the external boundary

This is the system's outermost boundary — it dials arbitrary user-supplied URLs and runs a converter over untrusted bytes — so two gates are load-bearing:

- **Outbound egress gate** ([`utilities/egress.py`](utilities/egress.py)) — every outbound request is validated before it connects: resolved IPs are checked against a deny-set (non-global addresses plus explicit SSRF ranges — shared address space, cloud-metadata link-local, NAT64, 6to4), DNS is deadline-capped, the validated IP is the IP actually dialled (closing the classify-vs-connect TOCTOU window), and every redirect hop is re-validated with `https → http` downgrades refused.
  Violations raise `EgressPolicyError` ([`core/errors.py`](core/errors.py)) and are classified non-retryable: a policy-violating destination will not pass on a retry.
  Full rationale and the IP-classification decision live in the [network-egress-policy design doc](../../../.specs/changes/archive/2026-04-28-network-egress-policy/design.md).
- **Untrusted subprocess output** ([`processing/uploader.py`](processing/uploader.py)) — conversion runs in a spawned subprocess, and the parent uploader treats its `metadata.json` and emitted files as untrusted.
  Declared filenames are containment-checked against the workspace (rejecting path separators and `..`, then resolving symlinks) and opened `O_NOFOLLOW`, so a compromised subprocess cannot read or write outside its workspace; an escape raises `WorkspaceEscape`.
- **Page-referenced resource admission** ([`utilities/html_prefetch.py`](utilities/html_prefetch.py)) — the pipeline decides which resources an untrusted page may pull in, not the converter.
  Each `<img src>` is resolved against the source URL, fetched through the egress gate, and carried into the document as a `data:` URI; anything not admitted loses its `src` and is recorded with the class that dropped it.
  The converter is then configured so it can dereference no location at all — no remote fetch, no local fetch, no browser rendering — which is what covers the resource-bearing shapes admission does not rewrite (`<source srcset>`, `<object data>`, CSS `url()`, and any not yet invented).
  Enumerating those attributes instead would be a deny-list over an open set; see the comment in [`processing/converter.py`](processing/converter.py) for the three library facts to recheck whenever the pinned Docling version moves.

## References

- Shared runtime and stage contract: [`aizk.pipeline`](../pipeline/README.md); the reference `StageHandler` implementation is [`handler.py`](handler.py).
- Downstream consumers: [`aizk.chunking`](../chunking/README.md) and [`aizk.graph`](../graph/README.md).
- Security rationale: the [network-egress-policy design doc](../../../.specs/changes/archive/2026-04-28-network-egress-policy/design.md) (egress + IP classification).
