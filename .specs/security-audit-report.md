# Security Audit Report

- **Date:** 2026-04-25
- **Branch:** `feature/security-audit`
- **Reviewer:** Claude (Opus 4.7) under direction of repo owner
- **Status:** First systematic security pass — findings only; resolutions tracked separately as SDD changes.

## Scope

In scope this pass:

- `src/aizk/conversion/` (77 Python files: API, workers, adapters, storage, migrations, utilities, wiring)
- `src/aizk/utilities/` (11 Python files: shared helpers used across `aizk`)

Explicitly out of scope this pass:

- `src/aizk/ai/` — deprecated/vestigial; owner-acknowledged risk surface deferred.
- `src/aizk/core/`, `src/aizk/datamodel/`, `src/aizk/metrics/` — small, low-risk; deferred to a follow-up bundled pass.
- Container, Dockerfile, entrypoint, CI workflows — deferred.
- Dependency audit — covered separately by pip-audit (commit `29f9384`).
- Test code — excluded per audit policy.

## Methodology

1. Discovery pass: a senior-security-engineer-styled subagent enumerated candidate findings in each subsystem, focused on input-validation, injection, deserialization, path traversal, SSRF, and crypto/secrets categories.
2. Validation pass: each candidate finding ≥ confidence 7 was independently re-read against current code by a second subagent, with attention to the actual end-to-end attack chain (source → sink, with current ingress gates).
3. Threshold: only findings with validated confidence ≥ 8 are reported below.
   One LOW finding (Vuln 3) is included despite below-threshold confidence because the owner elected to bundle it into the same hardening as Vuln 1/2.

## Summary of findings

| #   | Title                                                                             | File                                                                                | Severity | Confidence |
| --- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | -------- | ---------- |
| 1   | SSRF — worker URL fetcher has no host/IP/scheme filter                            | `src/aizk/conversion/adapters/fetchers/url.py`                                      | MEDIUM   | 8/10       |
| 2   | SSRF / blind LFI — Docling HTML backend with remote+local fetch enabled           | `src/aizk/conversion/workers/converter.py`                                          | LOW      | 8/10       |
| 3   | Path traversal — subprocess-supplied filenames consumed without containment check | `src/aizk/conversion/workers/uploader.py`, `src/aizk/conversion/utilities/paths.py` | LOW      | 7/10       |

Plus one posture observation (auth/deployment trust model) tracked separately.

---

## Vuln 1 — SSRF: worker URL fetcher has no host/IP/scheme filter

- **Files:**
  - `src/aizk/conversion/adapters/fetchers/url.py:80-119` — `UrlFetcher._fetch_http`
  - `src/aizk/conversion/adapters/fetchers/karakeep.py:105-111` — `KarakeepBookmarkResolver` Step 5
  - `src/aizk/conversion/adapters/fetchers/arxiv.py:71-76` — `ArxivFetcher._fetch_url`
- **Sink lookup helper used at exfil:** `src/aizk/conversion/api/routes/outputs.py:57-66`
- **Severity:** MEDIUM (today; would be HIGH on public ingress)
- **Category:** `ssrf`
- **Confidence:** 8/10

### Description

`UrlFetcher._fetch_http` calls `httpx.AsyncClient(follow_redirects=True).stream("GET", url)` on whatever URL the resolver hands it.
There is no allowlist, no IP-range block, no scheme restriction, and no per-redirect-hop validation.
Both host and protocol are attacker-controllable.

The attacker delivery path is wired today via the KaraKeep resolver: `KarakeepBookmarkResolver` Step 5 builds `UrlRef(url=source_url)` directly from the `source_url` field of the KaraKeep bookmark JSON.
A second sink with the same defect exists in `ArxivFetcher._fetch_url`.

The fetched bytes are converted to Markdown by Docling and uploaded to S3, then served unauthenticated at `GET /v1/outputs/{id}/markdown` — so this is a response-exfiltration SSRF, not blind.

### Exploit scenario

1. Attacker stores a KaraKeep bookmark whose `source_url` is `http://169.254.169.254/latest/meta-data/iam/security-credentials/<role>` (or any internal admin/health endpoint reachable by the worker network).
2. Attacker submits a `karakeep_bookmark` job referencing that bookmark via the conversion API (the only `_API_SUBMITTABLE_KIND` today, per `src/aizk/conversion/wiring/ingress_policy.py:16`).
3. KaraKeep resolver Step 5 returns `UrlRef(url=source_url)`; `UrlFetcher` issues the GET against the metadata service.
4. The IAM-credentials JSON is handed to Docling, which renders the text into Markdown.
5. Attacker calls `GET /v1/outputs/{id}/markdown` and reads the IAM keys.

The same primitive lets an attacker probe internal services, read internal admin pages, and (via redirects from any benign-looking URL) reach the metadata service even if the initial host looks public.

---

## Vuln 2 — SSRF / blind LFI: Docling HTML backend with `enable_remote_fetch=True` + `enable_local_fetch=True`

- **File:** `src/aizk/conversion/workers/converter.py:176-184` — `_create_document_converter`
- **Library reference:** `docling/backend/html_backend.py:4182-4231` (third-party; behavior verified during validation)
- **HTML entry path:** `src/aizk/conversion/workers/converter.py:433-470` (`convert_html`)
- **Subprocess wrapper:** `src/aizk/conversion/workers/orchestrator.py:169-277`
- **Inline-HTML production:** `src/aizk/conversion/adapters/fetchers/karakeep.py:110-117`
- **Severity:** LOW (gated on Vuln 1's HTML delivery; bytes-exfil blocked by `Image.open` rejection of non-image data; only side-channel leakage remains)
- **Category:** `ssrf` / `lfi`
- **Confidence:** 8/10

### Description

`_create_document_converter` constructs `HTMLBackendOptions(kind="html", fetch_images=True, enable_remote_fetch=True, enable_local_fetch=True, ...)`.
Docling's HTML backend honors these literally:

- For `<img src="http(s)://…">` it issues `requests.get(src_loc, stream=True)` with no host filter.
- For `<img src="file:///…">` or `<img src="/abs/path">` it calls `open(src_loc, "rb")` and reads the file.

Attacker-controlled HTML reaches this converter via:

1. The Vuln 1 chain (worker fetches attacker-controlled page, hands HTML to Docling).
2. KaraKeep `InlineHtmlRef` wrapping (when applicable).
3. Future widening of `_API_SUBMITTABLE_KINDS` to `inline_html` (per project memory).

The conversion subprocess uses `mp.get_context("spawn")` and `os.setpgrp()` only — no chroot, no namespace, no UID drop, no egress filter.
The worker's full network and filesystem are reachable from inside the converter.

### Exploit scenario

1. Attacker delivers HTML containing `<img src="http://169.254.169.254/latest/meta-data/...">`.
   A real outbound HTTP request fires from the worker even though the response bytes don't pass `Image.open` and aren't embedded in output.
   Result: blind SSRF against internal services and cloud metadata.
2. Attacker delivers `<img src="/etc/ssh/ssh_host_rsa_key">`.
   The file is opened and read; bytes fail `Image.open`, so contents are not exfiltrated, but existence/readability leak via the `DoclingError` message that `handle_job_error` persists into `jobs.error_message` (`src/aizk/conversion/workers/orchestrator.py:548`).
3. Cumulative effect: blind SSRF against internal services plus a low-bandwidth oracle for filesystem reconnaissance.

---

## Vuln 3 — Path traversal: subprocess-supplied filenames consumed without containment check

- **Files:**
  - `src/aizk/conversion/workers/uploader.py:40-47, 103-111` — `_upload_converted` consumes `metadata["markdown_filename"]` and `metadata["figure_files"]`
  - `src/aizk/conversion/utilities/paths.py:17-29` — `markdown_path` / `figure_paths` helpers
- **Severity:** LOW (defense-in-depth; no current attacker pathway)
- **Category:** `path_traversal`
- **Confidence:** 7/10

### Description

`_upload_converted` reads `metadata.json` written by the conversion subprocess and uses `metadata["markdown_filename"]` and `metadata["figure_files"]` directly via `markdown_path(workspace, markdown_filename)` / `figure_paths(workspace, figure_files)`.
Those helpers do `workspace / name` with no normalization or containment check.
The trust boundary between the subprocess (which produces these fields) and the parent uploader (which consumes them) is unguarded.

### Exploit scenario

Today the subprocess writes only the hardcoded `OUTPUT_MARKDOWN_FILENAME = "output.md"` and Docling-generated `figure-NNN.png` names, so this is not reachable.
The risk is the missing containment check at the trust boundary: a future Docling adapter, plugin, or supply-chain compromise that causes the subprocess to write `markdown_filename = "../../etc/hostname"` (or an absolute path) would cause the parent process to read that arbitrary file from disk and upload its bytes to S3 under `{aizk_uuid}/{markdown_filename}` — `Path.__truediv__` with an absolute name yields the absolute path, and the resulting `markdown_key` (`f"{prefix}/{markdown_filename}"`) is a string concat that escapes the prefix.

Included in the report despite below-threshold confidence because the owner elected to bundle the fix into the same hardening change as Vulns 1 and 2 (single trust-boundary cleanup).

---

## Posture observation — Deployment trust model and authentication

- **Files:** `src/aizk/conversion/api/main.py` (whole), all routes under `src/aizk/conversion/api/routes/`
- **Status:** Observation, not a finding.
  Owner-acknowledged.
- **Memory reference:** `project_api_auth_deployment.md`

### Description

The FastAPI conversion API has zero application-layer authentication on any route — no `Depends(auth_*)`, no bearer-token middleware, no Host-header allowlist, no CORS configuration.
The lifespan in `api/main.py` wires no auth middleware.
All routes (`/v1/jobs`, `/v1/bookmarks`, `/v1/outputs/*`, `/ui/*`, `/health`) accept any request.

The owner's intent is to publish the project for self-hosted deployment via container (Docker/Podman/k3s), with reverse proxy / ingress / firewall providing WAN access.
Today the deployment is internal-only and single-user; multi-user-readiness affordances are desired but not yet required.

### Exposure surface

- Unauthenticated access to job submission, bookmark management, output retrieval, and UI from any source that can reach the FastAPI port.
- DNS-rebinding-class threats applicable to localhost/LAN deploys: a browser tab on a network reachable to the service can issue same-origin requests via DNS rebinding, regardless of network ACLs.
- No identity recorded on any persisted artifact (`sources`, `conversion_jobs`, `conversion_outputs`), so multi-user enforcement cannot be added without a schema change.
- No CSRF protection on UI mutating routes; not exploitable today (no auth + same-origin assumption), but becomes a concern when auth lands.

### Notes

- The widening of `_API_SUBMITTABLE_KINDS` to include `arxiv`, `url`, `github_readme`, and `inline_html` (per project memory) widens the SSRF blast radius described in Vulns 1 and 2 by removing the KaraKeep-resolver indirection currently required to reach the worker fetchers.
- Resolution of this posture observation is being scoped as a separate SDD change (not bundled with the SSRF/path-traversal hardening).

---

## Negative findings (verified safe)

The following were checked carefully and produced no flags.

### Conversion subsystem

- **XXE in arxiv XML parsing** — `utilities/arxiv_utils.py` uses `defusedxml.ElementTree.fromstring`.
  Safe.
- **SQL injection** — every `sqlalchemy.text(...)` call uses bound parameters; alembic migrations build identifier-only SQL from compile-time constants.
- **Command injection** — only `utilities/litestream.py` shells out, with argv as a fixed list and `shell=False`.
  The `singlefile` fetcher is currently a `NotImplementedError` stub.
- **Pickle / yaml.load / eval** — none. `yaml.safe_dump` is used.
- **Template injection** — `Jinja2Templates` with default autoescape; templates contain no `|safe` filters.
- **Path traversal in `/v1/outputs/{id}/figures/{filename}`** — `api/routes/outputs.py:77-81` rejects any `/` in `filename`; S3 keys are not filesystem paths so `..` cannot escape the prefix server-side.
- **TLS verification disabled / hardcoded secrets** — none in this subsystem.
- **Open redirect** — only `RedirectResponse(url="/ui/jobs")` (fixed string) in `api/main.py`.

### Utilities subsystem

- **`process.py`** — name is misleading; contains zero `subprocess`/shell calls.
  Only an env-var context manager and a psutil-based process killer matching against hardcoded internal names.
- **`parse.py`** — pure regex/JSON helpers; no `yaml.load`, no `pickle`, no `eval`/`exec`, no XML parsing, no `marshal`.
  Callers use `json.loads`.
- **`url_utils.py`** — used as a syntactic validator and normalizer.
  Regex limited to `https?`/`ftps?`.
  **Important:** `url_utils.validate_url` is NOT a SSRF defense — it's purely syntactic (regex + `pydantic.HttpUrl` + `validators.url`); none of those reject loopback / RFC1918 / link-local / metadata addresses.
  Adopting this helper more broadly will not close the Vuln 1 gap.
- **`mlflow_tracing.py`** — explicit `_SENSITIVE_KEY_FRAGMENTS` and `_RAW_PAYLOAD_KEYS` redaction; tracking URI from configured value or env var (operator-controlled); MLflow span errors caught and logged at `debug`/`warning` without payload echo.
- **`log_utils.py`** — pure logging plumbing; no user data flows in.
- **`file_utils.py`** — `to_valid_fname` strips control chars / Windows-reserved chars / leading-trailing dots; `AtomicWriter` uses `NamedTemporaryFile` in the same parent directory + `os.replace`.
- **`path_utils.py`** — `path_is_valid` lacks `resolve() + is_relative_to(base)` containment, but conversion does not import `path_utils`, so there's no exploit chain through this helper today. `get_repo_path` uses `subprocess` with argv list, `shell=False`, and no user-controlled args.
- **`async_utils.py`, `batch_utils.py`, `limiters.py`** — control-flow / orchestration only. `batch_utils.py` does `json.loads` only on local files it produced.
