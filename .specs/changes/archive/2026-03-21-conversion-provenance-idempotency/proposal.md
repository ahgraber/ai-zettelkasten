# Proposal: conversion-provenance-idempotency

## Intent

The constitution requires every artifact to carry a replayable link to its extraction process and for raw inputs of lossy transforms to be recoverable.
The conversion worker currently stores neither the authoritative-source relationship to KaraKeep nor the full configuration needed to replay a conversion.
Additionally, the idempotency key omits the chat completions endpoint, which controls whether figures receive LLM-generated descriptions — a material difference in output.

## Scope

**In scope:**

- Declare KaraKeep as the authoritative raw input store and `karakeep_id` as the durable provenance
  reference in the spec
- Add `picture_description_enabled` (bool derived from whether `chat_completions_base_url` is set)
  to the idempotency key computation
- Persist the full Docling config payload used for a conversion in the S3 manifest so the conversion
  can be replayed with identical parameters

**Out of scope:**

- Storing raw HTML/PDF bytes locally (KaraKeep is the authoritative store; local copies not required)
- Semantic hashing of outputs
- Integrity checksums on intermediate artifacts (figures)
- Changes to the conversion-api spec

## Approach

Add `picture_description_enabled` (bool derived from whether `chat_completions_base_url` is set) to the idempotency key inputs in both spec and code.
Update the manifest schema to include the full Docling config snapshot (the same fields already extracted for idempotency hashing).
Add a spec requirement explicitly declaring KaraKeep as the authoritative source for raw inputs and `karakeep_id` as the durable provenance reference, satisfying the constitution's "MAY remain in an authoritative external system" clause.
