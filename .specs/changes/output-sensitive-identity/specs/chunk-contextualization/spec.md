# Delta for chunk-contextualization

## ADDED Requirements

### Requirement: Equivalent regenerated inputs reuse the active contextualization run without model calls

When the summary and chunking generations for a source regenerate and the contextualization inputs — the summary text and the ordered chunk content — are byte-equivalent to what the active contextualization run consumed, under unchanged contextualization-owned configuration, the stage SHALL retain the active run and its variants, record an applicability relation binding the new summary and chunking generations as one input set, and invoke the per-chunk model zero times.
The relation SHALL bind both generations together, so a query cannot combine a summary from one input set with chunks from another; a bound pair SHALL be valid only when its summary and chunking generations resolve to one conversion output — the same current output, or both applicable to the same output.
When the summary text or any ordered chunk content differs, or the contextualization-owned configuration has changed, the stage SHALL produce a new run under the existing supersession contract.

Serves: avoid-redundant-inference

#### Scenario: Byte-identical regenerated inputs are reconciled without per-chunk calls

- **GIVEN** a source whose Markdown change produced new chunking and summary generations, where the new summary text and the ordered chunk content are byte-equivalent to what the active contextualization run consumed
- **WHEN** the stage reconciles the source
- **THEN** the active contextualization run and its variants are retained, one applicability relation binds the new summary and chunking generations, and the per-chunk model is invoked zero times

#### Scenario: Changed summary text produces a new run

- **GIVEN** a regenerated summary whose text differs from what the active contextualization run consumed
- **WHEN** the stage processes the source
- **THEN** a new contextualization run supersedes the active one

#### Scenario: Changed chunk content produces a new run

- **GIVEN** a regenerated chunking generation in which any chunk's content differs from what the active contextualization run consumed
- **WHEN** the stage processes the source
- **THEN** a new contextualization run supersedes the active one

#### Scenario: The applicability relation is not split across input sets

- **GIVEN** an applicability relation candidate naming a summary generation from one input set and a chunking generation from another
- **WHEN** the relation is written
- **THEN** the write is rejected

### Requirement: A contextualization run records its consumed input pair as a stage-owned record

Every contextualization run SHALL persist, in the same transaction that records the run, an immutable run-level input record identifying the summary generation and chunking generation it consumed and their combined input fingerprint.
The record SHALL exist whether or not the run has variants: a document with zero chunks still records its inputs.
Every variant row's per-row provenance SHALL agree with its run's input record.
The recorded pair SHALL be valid only when the summary and chunking generations resolve to one conversion output.
The record is authoritative for generation-level currentness; it SHALL never be mutated, including when the run is later reused through applicability.

Serves: avoid-redundant-inference, rebuildable-corpus

#### Scenario: A zero-chunk run still records its inputs

- **GIVEN** a document whose chunking generation contains no chunks
- **WHEN** its contextualization run is recorded
- **THEN** the run's input record exists and identifies the summary and chunking generations consumed

#### Scenario: A variant naming a different input pair is rejected

- **GIVEN** a contextualization run with its input record
- **WHEN** a variant row naming a different summary or chunking generation is written under that run
- **THEN** the write is rejected

#### Scenario: Reuse leaves the input record unchanged

- **GIVEN** a contextualization run reused for a newer equivalent input set via an applicability relation
- **WHEN** its input record is inspected
- **THEN** it still identifies the originally consumed generations, unchanged

## MODIFIED Requirements

### Requirement: Each chunk has a run-scoped contextualized variant carrying its derivation key

> Previously: the variant's derivation key was defined over input identities — the summary identity, working chunk identity, and neighbor identities — so a regenerated summary or chunking generation with unchanged content still changed the key and superseded the variant.

For any persisted chunk processed in a contextualization run, the run SHALL persist one contextualized variant for that chunk, carrying the source `chunk_id`, the `context_version`, and a derivation key over the contextualization inputs the variant consumed — at minimum the document summary text, the working chunk content, the two-prior/one-next neighbor content used to build it, the `splitter_version` of the chunking generation read, the context-window policy, the contextualization prompt identity, and the model profile.
The key is a fingerprint of consumed content and stage-owned configuration; it SHALL NOT embed the summary run, chunking run, or any other upstream run identity or key.
The variant SHALL also record, as provenance distinct from the derivation key, a pointer to the summary it read and to the exact chunking generation it read its chunks from (so the variant resolves to one unambiguous manifest, since a `chunk_id` may appear in many generations).
A variant SHALL be separately addressable from the chunk.
Re-processing a chunk whose consumed content and `context_version` are unchanged SHALL NOT create a duplicate; a change to a neighboring chunk's content, to the summary text, to the prompt/window/model inputs, to the `splitter_version`, or to the `context_version` SHALL produce a new run's variant that supersedes the prior.
A regenerated summary or chunking generation whose consumed content is unchanged SHALL NOT change the key.

Serves: avoid-redundant-inference

#### Scenario: A processed chunk yields one provenance-and-derivation-key-bearing variant

- **GIVEN** a persisted chunk
- **WHEN** contextualization processes it
- **THEN** a contextualized variant exists that is separately addressable and carries the source `chunk_id`, `context_version`, a derivation key over the summary text, working chunk content, 2p/1n neighboring chunk content, `splitter_version`, window policy, prompt identity, and model profile used, and provenance pointers to the summary and the chunking generation it read

#### Scenario: A changed splitter version produces a superseding variant under the same context version

- **GIVEN** a chunk with a persisted variant, whose source is then re-chunked under a new `splitter_version` while `context_version` stays the same
- **WHEN** contextualization reprocesses the chunk
- **THEN** a new run's variant is created recording the new derivation key, and the prior variant remains present and unmodified

#### Scenario: A changed neighbor produces a superseding variant under the same version

- **GIVEN** a chunk with a persisted variant, whose neighboring chunk's content then changes while `context_version` stays the same
- **WHEN** contextualization reprocesses the chunk
- **THEN** a new run's variant is created recording the new derivation key, and the prior variant remains present and unmodified

#### Scenario: Unchanged inputs and version do not duplicate the variant

- **GIVEN** a chunk with a persisted variant at a given derivation key and `context_version`
- **WHEN** contextualization reprocesses it with both unchanged
- **THEN** no duplicate variant is created

#### Scenario: A regenerated upstream with unchanged content leaves the key unchanged

- **GIVEN** a chunk with a persisted variant, whose summary and chunking generations are then superseded by generations with byte-equivalent summary text and chunk content
- **WHEN** the variant's derivation key for the current inputs is computed
- **THEN** it equals the persisted variant's key
