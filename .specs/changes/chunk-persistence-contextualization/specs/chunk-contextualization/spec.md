# Delta for chunk-contextualization

## ADDED Requirements

### Requirement: A document is summarized within a contextualization run keyed by its inputs

For any document processed, a contextualization run SHALL persist exactly one summary for that document, and the run SHALL record the input fingerprint that produced it — at minimum the document's source markdown hash — together with its `summary_version`.
Re-processing a document whose source markdown hash and `summary_version` are both unchanged SHALL NOT create a new run or a duplicate summary.
A change to either the source markdown or the `summary_version` SHALL produce a new run that supersedes the prior one; the prior run's summary record SHALL remain present and unmodified.

#### Scenario: A processed document yields one summary with input fingerprint and version

- **GIVEN** a document that has been chunked and persisted
- **WHEN** contextualization processes it
- **THEN** exactly one summary exists for that document in the active run, recording the source markdown hash and `summary_version` that produced it

#### Scenario: Unchanged inputs and version do not create a new run

- **GIVEN** a document already summarized at a given markdown hash and `summary_version`
- **WHEN** contextualization processes it again with both unchanged
- **THEN** no new run and no duplicate summary are created

#### Scenario: Changed source markdown produces a superseding run

- **GIVEN** a document already summarized, whose source markdown then changes while `summary_version` stays the same
- **WHEN** contextualization reprocesses it
- **THEN** a new run is created whose summary records the new markdown hash, the prior run is marked superseded, and the prior summary record remains present and unmodified

### Requirement: Each chunk has a run-scoped contextualized variant carrying its input fingerprint

For any persisted chunk processed in a contextualization run, the run SHALL persist one contextualized variant for that chunk, carrying the source `chunk_id`, the `context_version`, and a fingerprint of the contextualization inputs — at minimum the document summary identity and the neighboring chunk identities used to build it.
A variant SHALL be separately addressable from the chunk.
Re-processing a chunk whose contextualization inputs and `context_version` are unchanged SHALL NOT create a duplicate; a change to a neighboring chunk, to the summary, or to the `context_version` SHALL produce a new run's variant that supersedes the prior.

#### Scenario: A processed chunk yields one provenance-and-fingerprint-bearing variant

- **GIVEN** a persisted chunk
- **WHEN** contextualization processes it
- **THEN** a contextualized variant exists that is separately addressable and carries the source `chunk_id`, `context_version`, and a fingerprint of the summary and neighboring chunks used

#### Scenario: A changed neighbor produces a superseding variant under the same version

- **GIVEN** a chunk with a persisted variant, whose neighboring chunk then changes while `context_version` stays the same
- **WHEN** contextualization reprocesses the chunk
- **THEN** a new run's variant is created recording the new input fingerprint, and the prior variant remains present and unmodified

#### Scenario: Unchanged inputs and version do not duplicate the variant

- **GIVEN** a chunk with a persisted variant at a given input fingerprint and `context_version`
- **WHEN** contextualization reprocesses it with both unchanged
- **THEN** no duplicate variant is created

### Requirement: Contextualization does not modify the source chunk

Contextualization SHALL NOT modify the persisted chunk it reads.
A chunk's stored `text`, `content_hash`, and `chunk_id` SHALL be equal before and after contextualization; the contextualized variant SHALL be additive and stored apart from the chunk.

#### Scenario: Source chunk is unchanged after contextualization

- **GIVEN** a persisted chunk with known `text`, `content_hash`, and `chunk_id`
- **WHEN** contextualization processes the chunk and persists its variant
- **THEN** the chunk's stored `text`, `content_hash`, and `chunk_id` are unchanged from before contextualization

### Requirement: The contextualized variant is self-contained

A chunk's contextualized variant SHALL be self-contained: any reference in the chunk whose referent lies outside the chunk — in the document summary or an adjacent chunk — SHALL be resolved to an explicit referent in the variant, so the variant can be interpreted without access to the neighboring chunks.

#### Scenario: A cross-chunk reference is resolved in the variant

- **GIVEN** a chunk containing a reference (such as a pronoun or definite phrase) whose referent appears in the document summary or an adjacent chunk
- **WHEN** contextualization produces the chunk's variant
- **THEN** the variant names that referent explicitly rather than leaving the reference unresolved

### Requirement: Contextualization output is independent of run mode

For any document, contextualization SHALL produce the same run records — one summary per document and one contextualized variant per persisted chunk, with identical provenance and input-fingerprint linkage — whether the document is processed in bulk/backfill mode or incremental mode.
Run mode SHALL affect only batching and scheduling, never which records are produced or their provenance.

#### Scenario: Bulk and incremental processing produce the same records

- **GIVEN** a document processed in bulk/backfill mode and the same document processed in incremental mode under the same inputs and versions
- **WHEN** the resulting run's summary and contextualized-variant records are compared by count, provenance, and input fingerprint
- **THEN** both modes yield one summary for the document and one variant per persisted chunk with identical linkage

#### Scenario: Incremental single-document processing matches the bulk record shape

- **GIVEN** a single document ingested incrementally
- **WHEN** its summary and variant records are inspected
- **THEN** they carry the same record shape, provenance, and input fingerprint that bulk processing of that document would produce

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
