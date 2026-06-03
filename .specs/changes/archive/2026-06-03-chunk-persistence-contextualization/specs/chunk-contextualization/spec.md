# Delta for chunk-contextualization

## ADDED Requirements

### Requirement: A document is summarized within a contextualization run keyed by its inputs

For any document processed, a contextualization run SHALL persist exactly one summary for that document, and the run SHALL record the derivation key that produced it — at minimum the document's source markdown hash, summary prompt identity, and model profile — together with its `summary_version`.
The run SHALL also record, as provenance distinct from the derivation key, a locator to the exact Markdown it consumed, so the summary's input is retrievable as well as verifiable.
Re-processing a document whose source markdown hash, derivation-key prompt/model inputs, and `summary_version` are all unchanged SHALL NOT create a new run or a duplicate summary.
A change to the source markdown, derivation-key prompt/model inputs, or the `summary_version` SHALL produce a new run that supersedes the prior one; the prior run's summary record SHALL remain present and unmodified.

#### Scenario: A processed document yields one summary with derivation key and version

- **GIVEN** a document that has been chunked and persisted
- **WHEN** contextualization processes it
- **THEN** exactly one summary exists for that document in the active run, recording the source markdown hash, prompt identity, model profile, and `summary_version` that produced it

#### Scenario: Unchanged inputs and version do not create a new run

- **GIVEN** a document already summarized at a given markdown hash, summary prompt identity, model profile, and `summary_version`
- **WHEN** contextualization processes it again with both unchanged
- **THEN** no new run and no duplicate summary are created

#### Scenario: Changed source markdown produces a superseding run

- **GIVEN** a document already summarized, whose source markdown then changes while `summary_version` stays the same
- **WHEN** contextualization reprocesses it
- **THEN** a new run is created whose summary records the new markdown hash, the prior run is marked superseded, and the prior summary record remains present and unmodified

### Requirement: Each chunk has a run-scoped contextualized variant carrying its derivation key

For any persisted chunk processed in a contextualization run, the run SHALL persist one contextualized variant for that chunk, carrying the source `chunk_id`, the `context_version`, and a derivation key for the contextualization inputs — at minimum the document summary identity, the working chunk identity, the two-prior/one-next neighbor identities used to build it, the `splitter_version` of the chunking generation read, the context-window policy, the contextualization prompt identity, and the model profile.
The variant SHALL also record, as provenance distinct from the derivation key, a pointer to the summary it read and to the exact chunking generation it read its chunks from (so the variant resolves to one unambiguous manifest, since a `chunk_id` may appear in many generations).
A variant SHALL be separately addressable from the chunk.
Re-processing a chunk whose contextualization inputs and `context_version` are unchanged SHALL NOT create a duplicate; a change to a neighboring chunk, to the summary, to the prompt/window/model inputs, to the `splitter_version`, or to the `context_version` SHALL produce a new run's variant that supersedes the prior.

#### Scenario: A processed chunk yields one provenance-and-derivation-key-bearing variant

- **GIVEN** a persisted chunk
- **WHEN** contextualization processes it
- **THEN** a contextualized variant exists that is separately addressable and carries the source `chunk_id`, `context_version`, a derivation key for the summary, working chunk, 2p/1n neighboring chunks, `splitter_version`, window policy, prompt identity, and model profile used, and provenance pointers to the summary and the chunking generation it read

#### Scenario: A changed splitter version produces a superseding variant under the same context version

- **GIVEN** a chunk with a persisted variant, whose source is then re-chunked under a new `splitter_version` while `context_version` stays the same
- **WHEN** contextualization reprocesses the chunk
- **THEN** a new run's variant is created recording the new derivation key, and the prior variant remains present and unmodified

#### Scenario: A changed neighbor produces a superseding variant under the same version

- **GIVEN** a chunk with a persisted variant, whose neighboring chunk then changes while `context_version` stays the same
- **WHEN** contextualization reprocesses the chunk
- **THEN** a new run's variant is created recording the new derivation key, and the prior variant remains present and unmodified

#### Scenario: Unchanged inputs and version do not duplicate the variant

- **GIVEN** a chunk with a persisted variant at a given derivation key and `context_version`
- **WHEN** contextualization reprocesses it with both unchanged
- **THEN** no duplicate variant is created

### Requirement: Contextualization does not modify the source chunk

Contextualization SHALL NOT modify the persisted chunk it reads.
A chunk's stored `text`, `content_hash`, and `chunk_id` SHALL be equal before and after contextualization; the contextualized variant SHALL be a separate revision stored apart from the chunk, leaving the raw chunk the cited, source-faithful unit.

#### Scenario: Source chunk is unchanged after contextualization

- **GIVEN** a persisted chunk with known `text`, `content_hash`, and `chunk_id`
- **WHEN** contextualization processes the chunk and persists its variant
- **THEN** the chunk's stored `text`, `content_hash`, and `chunk_id` are unchanged from before contextualization

### Requirement: The contextualized variant is a self-contained revision

A chunk's contextualized variant SHALL be a revision of the working chunk: the chunk rewritten so that any reference whose referent lies outside the chunk — in the document summary or a neighboring chunk — is resolved to an explicit referent inline, so the revision can be interpreted without access to the neighboring chunks.
The revision SHALL be grounded strictly in the provided document summary and neighboring chunks, SHALL NOT introduce facts or entities that are not supported by those inputs, and SHALL NOT add, drop, or alter any claim of the working chunk.
When those inputs conflict, contextualization SHALL prefer evidence in this order: working chunk, nearest neighboring chunks, farther neighboring chunks, document summary.
A reference that cannot be resolved from the provided inputs SHALL be left unchanged rather than guessed.
If the working chunk is already self-contained, the revision MAY be empty and the consumed contextualized text SHALL equal the raw chunk text.

#### Scenario: A cross-chunk reference is resolved in the revision

- **GIVEN** a chunk containing a reference (such as a pronoun or definite phrase) whose referent appears in the document summary or an adjacent chunk
- **WHEN** contextualization produces the chunk's variant
- **THEN** the stored revision names that referent explicitly in place of the reference, while the raw chunk text remains unchanged

#### Scenario: A self-contained chunk can produce an empty revision

- **GIVEN** a chunk that does not need any reference resolved
- **WHEN** contextualization produces the chunk's variant
- **THEN** the stored revision may be empty and the consumed contextualized text equals the raw chunk text unchanged

#### Scenario: Contextualization rejects runaway output

- **GIVEN** a model output substantially longer than the working chunk's chunk-relative revision budget
- **WHEN** contextualization validates the output before persistence
- **THEN** the output is rejected and is not persisted as a contextualized variant

### Requirement: A contextualized variant is fully traceable to the inputs it consumed

Given a contextualized variant, it SHALL be possible to recover, one stage at a time, the exact inputs that produced it: the source chunk text and the chunk's `span` in the generation read, the chunking generation and the source markdown it consumed, the document summary, and the durable source identity (`aizk_uuid`) the whole chain belongs to.
Each edge in this chain SHALL record enough identity, locator, and verification fingerprint to retrieve and verify the exact input content consumed at that edge, using recorded provenance rather than re-running a non-deterministic or version-dependent producer.

#### Scenario: A variant traces back to its source text and source identity

- **GIVEN** a persisted contextualized variant
- **WHEN** its recorded provenance is followed backward
- **THEN** it resolves to the exact chunking generation it read, that generation's chunk identity and `span` (hence the raw chunk text), the document summary it used, the source markdown that generation consumed (retrievable and hash-verifiable), and the `aizk_uuid` the chain belongs to

### Requirement: Contextualization prompts are grounded and data-safe

Contextualization SHALL instruct the model to use only the provided document and chunk text, to output only the requested summary or blurb without labels or metadata, and to treat source document/chunk text as untrusted data rather than instructions.
The prompt envelope SHALL prevent source text that resembles prompt delimiters from changing the structure of the prompt.

#### Scenario: Delimiter-looking source text remains data

- **GIVEN** a chunk whose text contains a string that resembles a prompt delimiter
- **WHEN** contextualization builds the model prompt
- **THEN** that string is represented as source data and does not appear as a live prompt delimiter

### Requirement: Contextualization output is independent of run mode

For any document, contextualization SHALL produce the same run records — one summary per document and one contextualized variant per persisted chunk, with identical provenance and derivation-key linkage — whether the document is processed in bulk/backfill mode or incremental mode.
Run mode SHALL affect only batching and scheduling, never which records are produced or their provenance.

#### Scenario: Bulk and incremental processing produce the same records

- **GIVEN** a document processed in bulk/backfill mode and the same document processed in incremental mode under the same inputs and versions
- **WHEN** the resulting run's summary and contextualized-variant records are compared by count, provenance, and derivation key
- **THEN** both modes yield one summary for the document and one variant per persisted chunk with identical linkage

#### Scenario: Incremental single-document processing matches the bulk record shape

- **GIVEN** a single document ingested incrementally
- **WHEN** its summary and variant records are inspected
- **THEN** they carry the same record shape, provenance, and derivation key that bulk processing of that document would produce

### Requirement: Contextualization processes each document as an idempotent, retryable work-unit

Contextualization SHALL process each document as a discrete work-unit with a durable lifecycle status, claimable for exclusive processing so two workers do not process the same document concurrently.
A work-unit SHALL be enqueued in either bulk/backfill or incremental form and SHALL be executable at-least-once: re-executing a work-unit whose derivation-key inputs and versions are unchanged SHALL NOT create a new run or any duplicate summary or variant record, and SHALL reach a succeeded terminal status.
A failed work-unit SHALL be classified as retryable (a transient model or I/O error) or permanent (input or model output that cannot be processed); retries SHALL be bounded so a persistently failing unit reaches a terminal failed status rather than retrying without limit.
A work-unit SHALL be cancellable and subject to a processing timeout, reaching a terminal status in every case.

#### Scenario: A re-executed work-unit produces no duplicate records

- **GIVEN** a document already processed to completion at a given derivation key and versions
- **WHEN** its work-unit is executed again with both unchanged (for example, after an interrupted run is recovered)
- **THEN** no new run and no duplicate summary or variant are created, and the work-unit reaches a succeeded terminal status

#### Scenario: A transient failure is retried within a bound; a permanent failure is not

- **GIVEN** a work-unit whose processing fails
- **WHEN** the failure is classified as retryable
- **THEN** it is eligible for re-processing up to the retry bound, after which it reaches a terminal failed status; a failure classified as permanent is not retried

### Requirement: The contextualization model is a substitutable dependency

Contextualization SHALL access the model that produces the document summary and the contextualized variants as a substitutable dependency reached through a single access point.
A substitute model — for example, a deterministic test double — SHALL be usable in place of the production model without changing contextualization's logic or any other requirement in this spec.
Every model invocation the stage makes SHALL pass through that single access point.

#### Scenario: A substitute model is used without changing stage logic

- **GIVEN** a deterministic substitute model supplied in place of the production model
- **WHEN** contextualization processes a document and its chunks
- **THEN** the run's summary and contextualized variants are produced using the substitute, with no change to contextualization logic and with the record shape and provenance the other requirements specify

#### Scenario: Every model invocation passes through the single access point

- **GIVEN** contextualization configured with a model that records each invocation it receives
- **WHEN** a document and its chunks are processed
- **THEN** every model invocation the stage makes is observed at that access point, and the stage performs none outside it
