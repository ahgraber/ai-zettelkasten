# Delta for chunk-contextualization

## MODIFIED Requirements

### Requirement: A contextualized variant is fully traceable to the inputs it consumed

Given a contextualized variant, it SHALL be possible to recover, one stage at a time, the exact inputs that produced it: the source chunk text and the chunk's `span` in the generation read, the chunking generation and the source markdown it consumed, the document summary, and the durable source identity (`source_id`) the whole chain belongs to.
Each edge in this chain SHALL record enough identity, locator, and verification fingerprint to retrieve and verify the exact input content consumed at that edge, using recorded provenance rather than re-running a non-deterministic or version-dependent producer.

Serves: coherent-pipeline-foundation, portable-knowledge

> Previously: the durable source identity at the end of the chain was named `aizk_uuid`.

#### Scenario: A variant traces back to its source text and source identity

- **GIVEN** a persisted contextualized variant
- **WHEN** its recorded provenance is followed backward
- **THEN** it resolves to the exact chunking generation it read, that generation's chunk identity and `span` (hence the raw chunk text), the document summary it used, the source markdown that generation consumed (retrievable and hash-verifiable), and the `source_id` the chain belongs to

### Requirement: A retry of a partially-completed contextualization attempt re-invokes the model only for outputs not already retained

For a work-unit whose prior attempt validated and durably retained a model output for the document summary and/or some of its chunk revisions and then failed before completing, a retry under unchanged derivation-key inputs and versions SHALL NOT re-invoke the model for any summary or chunk revision whose valid output was already retained, and SHALL invoke the model only for those not yet retained.
A retained output is scoped to its source (`source_id`): a retained output for one source SHALL NOT be reused for a different source, even where an identical derivation key would otherwise match — a case that arises for the document summary, whose derivation key does not itself embed the source.
On successful completion, the persisted generation SHALL be identical — by count, provenance, and derivation-key linkage — to the generation an uninterrupted run under the same inputs and versions would produce.
This guarantee is independent of how far the prior attempt progressed: when every output was already retained, a re-execution SHALL invoke the model zero times.

Serves: coherent-pipeline-foundation

> Previously: the source that a retained output is scoped to was named `aizk_uuid`.

#### Scenario: A retry invokes the model only for the chunks not yet revised

- **GIVEN** a document whose first contextualization attempt validated and retained the document summary and the revisions for the first K of its N chunks, then failed before revising the remaining chunks
- **WHEN** the work-unit is retried under unchanged derivation-key inputs and versions
- **THEN** the model is invoked only for the N−K not-yet-revised chunks — not for the document summary and not for the first K chunks — and on success exactly one summary and one variant per chunk are persisted, matching an uninterrupted run by count, provenance, and derivation-key linkage

#### Scenario: A first-contextualization summary is reused across the retry so its revision inputs stay stable

- **GIVEN** a document with no prior active summary run, whose first attempt retained the summary and then failed during chunk revision
- **WHEN** the work-unit is retried under unchanged inputs
- **THEN** the model is not re-invoked for the summary, the persisted summary equals the one the first attempt retained, and the retry's chunk revisions are produced against that same summary

#### Scenario: Re-executing an already-completed generation invokes the model zero times

- **GIVEN** a document already contextualized to completion at a given derivation key and versions
- **WHEN** its work-unit is executed again with both unchanged
- **THEN** the model is invoked zero times, no new run and no duplicate summary or variant are created, and the work-unit reaches a succeeded terminal status

#### Scenario: Retained model work for one source is not reused for another

- **GIVEN** two distinct sources with byte-identical Markdown, where one source has validated and retained its document summary from a partial attempt
- **WHEN** the other source is contextualized under the same inputs and versions
- **THEN** the model is invoked for the other source's own summary rather than reusing the first source's retained summary
