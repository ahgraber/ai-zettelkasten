# Proposal: Docling Config Clarity

## Intent

The env vars controlling AI-powered figure description (`CHAT_COMPLETIONS_BASE_URL`, `CHAT_COMPLETIONS_API_KEY`, `DOCLING_VLM_MODEL`) do not convey their purpose and are not visually grouped with the other picture-description settings.
Operators cannot determine from `.env.example` alone which variables are related or what they do.
Additionally, the service starts successfully when the configured picture description endpoint is unreachable, deferring failure to job time with a cryptic HTTP error instead of failing fast at startup.

## Scope

**In scope:**

- Rename `CHAT_COMPLETIONS_BASE_URL` → `DOCLING_PICTURE_DESCRIPTION_BASE_URL`
- Rename `CHAT_COMPLETIONS_API_KEY` → `DOCLING_PICTURE_DESCRIPTION_API_KEY`
- Rename `DOCLING_VLM_MODEL` → `DOCLING_PICTURE_DESCRIPTION_MODEL`
- Update all Python field names, internal references, tests, and log strings to match
- Add `probe_picture_description(config)` as a fatal startup check (GET `/models`, 10s timeout)
- Add `_check_picture_description(config)` to the `/health/ready` readiness endpoint (GET `/models`, 5s timeout, conditional on configuration)
- Full rewrite of `.env.example`: named sections, all `ConversionConfig` fields present, provider examples for OpenRouter and local vLLM, `KARAKEEP_API_URL` typo fixed, `_UNDERSCORE_PREFIXED` preset pattern removed, `LOG_FORMAT` added

**Out of scope:**

- Adding a vLLM container to `podman-compose.yaml` (separate workstream)
- Any change to conversion logic, Docling pipeline options, or job processing behavior
- Supporting providers that do not implement the OpenAI `GET /models` endpoint

## Approach

The rename is a mechanical find-and-replace across `config.py`, `converter.py`, `startup.py`, `health.py`, `.env.example`, and all test files.
The startup probe follows the exact pattern of `probe_s3` and `probe_karakeep` in `startup.py`.
The health check follows the exact pattern of `_check_db` and `_check_s3` in `health.py`.
`.env.example` is rewritten from scratch with structured sections and inline provider examples.

This is a **breaking change** for existing deployments: any `.env` file using `CHAT_COMPLETIONS_BASE_URL`, `CHAT_COMPLETIONS_API_KEY`, or `DOCLING_VLM_MODEL` must be updated before restart.
