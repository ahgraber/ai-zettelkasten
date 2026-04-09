# Design: Docling Config Clarity

## Context

The conversion service configuration has two problems:

1. **Naming confusion.**
   The env vars that configure AI-powered figure description (`CHAT_COMPLETIONS_BASE_URL`, `CHAT_COMPLETIONS_API_KEY`, `DOCLING_VLM_MODEL`) do not convey their actual purpose.
   `CHAT_COMPLETIONS_*` sounds like a general-purpose LLM API selector; `DOCLING_VLM_MODEL` conflates docling's internal VLM pipeline mode with the external API model name.
   Operators reading `.env.example` cannot infer which vars are related without reading source code.

2. **No startup probe for the picture description endpoint.**
   The service starts successfully even when the configured endpoint is unreachable.
   Jobs then fail at conversion time with a cryptic HTTP error.
   This is inconsistent with how S3 and KaraKeep are treated (both have fatal startup probes).

A secondary problem: `.env.example` is incomplete and inconsistently documented — several config fields are absent, section headers are informal, and the `KARAKEEP_API_URL` comment contains a wrong variable name.

## Decisions

### Decision: Rename picture description env vars to `DOCLING_PICTURE_DESCRIPTION_*`

**Chosen:** Rename the three ambiguously-named fields:

| Old                         | New                                    |
| --------------------------- | -------------------------------------- |
| `CHAT_COMPLETIONS_BASE_URL` | `DOCLING_PICTURE_DESCRIPTION_BASE_URL` |
| `CHAT_COMPLETIONS_API_KEY`  | `DOCLING_PICTURE_DESCRIPTION_API_KEY`  |
| `DOCLING_VLM_MODEL`         | `DOCLING_PICTURE_DESCRIPTION_MODEL`    |

Python field names in `ConversionConfig` update to match: `docling_picture_description_base_url`, `docling_picture_description_api_key`, `docling_picture_description_model`.

**Rationale:** All six picture-description-related fields now share a consistent prefix (`DOCLING_PICTURE_DESCRIPTION_*` or `DOCLING_PICTURE_*`), making their relationship self-evident without comments.
The `DOCLING_` prefix is already the convention for all docling-related fields.

**Alternatives considered:**

- Keep `CHAT_COMPLETIONS_*` with added comments: comments drift; naming is the right tool.
- Use `DOCLING_VLM_*`: `VLM` already means docling's internal `VlmPipeline` mode in docling's own docs, adding confusion.

### Decision: Add `probe_picture_description` as a fatal startup check

**Chosen:** When `DOCLING_PICTURE_DESCRIPTION_BASE_URL` and `DOCLING_PICTURE_DESCRIPTION_API_KEY` are both set, `probe_picture_description(config)` issues `GET {base_url}/models` with an `Authorization: Bearer` header and a 10s timeout.
Non-2xx or connection error → `StartupValidationError`.
Called from `validate_startup()` after the existing probes.
If neither field is set, the probe is a no-op.

**Rationale:** `GET /models` is part of the OpenAI API specification and is implemented by all target providers (OpenRouter, vLLM, Ollama, llama.cpp).
It validates both reachability and authentication cheaply.
Consistent with the existing pattern for S3 and KaraKeep.

**Alternatives considered:**

- POST a real request: validates model load but is slow, expensive on commercial APIs, and requires a valid image.
- TCP connect only: doesn't validate API keys or route correctness.
- Warn-only: inconsistent with S3/KaraKeep treatment; operators configure the endpoint intentionally, so failure should be loud.

### Decision: Add `_check_picture_description` to the `/health/ready` endpoint

**Chosen:** When the picture description endpoint is configured, include a `CheckResult(name="picture_description")` in the readiness response.
Same `GET /models` probe, 5s timeout.
If the feature is not configured, the check is omitted entirely from results.
Non-reachable → `CheckResult(status="unavailable")` → 503 + warning log.

**Rationale:** Consistent with existing `_check_db` and `_check_s3` checks.
Allows monitoring systems to detect endpoint degradation after startup without cluttering responses for deployments that don't use picture description.

### Decision: Rewrite `.env.example` with complete documentation and clear sections

**Chosen:** Full rewrite with:

- Named sections for each subsystem: Core, Worker, Docling Pipeline, Docling Picture Description, API Server, Logging, Observability, Litestream
- All `ConversionConfig` fields present (uncommented if they have sensible defaults; commented out if rarely changed)
- Provider examples for OpenRouter and local vLLM in the picture description block (using `http://localhost:8000/v1`, not a compose service name — vLLM container definition is out of scope)
- `_UNDERSCORE_PREFIXED` preset pattern removed (was only used to feed `CHAT_COMPLETIONS_*`)
- `KARAKEEP_API_URL` typo fixed to `KARAKEEP_BASE_URL`
- `LOG_FORMAT` added

**Rationale:** Operators should be able to configure the service from `.env.example` alone without reading source code.
Removing the preset pattern eliminates indirection that only served the now-renamed `CHAT_COMPLETIONS_*` vars.

## Architecture

```text
ConversionConfig (config.py)
  docling_picture_description_base_url   DOCLING_PICTURE_DESCRIPTION_BASE_URL
  docling_picture_description_api_key    DOCLING_PICTURE_DESCRIPTION_API_KEY
  docling_picture_description_model      DOCLING_PICTURE_DESCRIPTION_MODEL
  docling_picture_timeout                DOCLING_PICTURE_TIMEOUT  (unchanged)
  docling_enable_picture_classification  DOCLING_ENABLE_PICTURE_CLASSIFICATION  (unchanged)
  is_picture_description_enabled()       checks base_url + api_key (field refs updated)

startup.py
  validate_startup(config, role)
    +---> probe_s3(config)
    +---> probe_karakeep()
    +---> probe_picture_description(config)   NEW
              if not configured: no-op
              GET {base_url}/models, Authorization: Bearer, 10s timeout
              non-2xx / error => StartupValidationError
    +---> log_feature_summary(config, role)   updated field refs + reason strings

health.py
  readiness()
    +---> _check_db(engine)
    +---> _check_s3(s3_client)
    +---> _check_picture_description(config)  NEW (conditional on config)
              GET {base_url}/models, Authorization: Bearer, 5s timeout
              non-2xx / error => CheckResult(status="unavailable")

converter.py
  _get_picture_description_options()     updated field refs
  _call_vlm_api()                        updated field refs
  _enrich_picture_descriptions()         updated field refs (logging)

.env.example                             full rewrite with sections + provider examples
```

## Out of Scope

- vLLM container definition in `podman-compose.yaml` — adding vLLM to the compose stack involves GPU passthrough, model volume mounts, image selection, and memory sizing; this is a separate workstream.

## Risks

- **Breaking change for existing deployments.**
  Operators with `CHAT_COMPLETIONS_BASE_URL` in production `.env` files must rename to `DOCLING_PICTURE_DESCRIPTION_BASE_URL`.
  This should be called out prominently in the commit message and any changelog.
- **`/models` endpoint not available on all providers.**
  Mitigation: all target providers (OpenRouter, vLLM, Ollama, llama.cpp) implement it.
  If a future provider does not, the probe can be skipped by unsetting the API key — but this would also disable picture description.
- **Startup time increase.**
  One additional HTTP probe adds up to 10s worst-case to startup.
  Acceptable given the existing pattern; probes run sequentially.
